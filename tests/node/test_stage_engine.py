"""Tests for mycelium.node.stage_engine."""

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

from transformers import Qwen2Config, Qwen2ForCausalLM  # noqa: E402

from mycelium.node import stage_engine  # noqa: E402
from mycelium.node.stage_engine import load_stage  # noqa: E402

NUM_LAYERS = 4


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    """A tiny random-weight Qwen2 saved as a sharded checkpoint, plus the
    unsplit model loaded from the same weights. Sharded on purpose so the
    selective-loading tests have more than one file to (not) read."""
    torch.manual_seed(0)
    config = Qwen2Config(
        vocab_size=128,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=NUM_LAYERS,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=32,
        tie_word_embeddings=False,
    )
    model = Qwen2ForCausalLM(config).eval()
    path = tmp_path_factory.mktemp("tiny-qwen2")
    model.save_pretrained(path, max_shard_size="50KB")
    return model, str(path)


def run_chain(model, stages, session_id, token_ids):
    """Embed on the 'client', push through every stage, apply final norm and
    LM head on the 'client' — the split the pipeline client (#81) will use."""
    hidden = model.model.embed_tokens(token_ids)
    for stage in stages:
        hidden = stage.forward(session_id, hidden)
    return model.lm_head(model.model.norm(hidden))


def two_stages(path):
    return [
        load_stage(path, 0, 1, dtype=torch.float32),
        load_stage(path, 2, NUM_LAYERS - 1, dtype=torch.float32),
    ]


@torch.no_grad()
def test_chained_stages_match_unsplit_model_on_prefill(reference):
    model, path = reference
    stages = two_stages(path)
    for stage in stages:
        stage.open_session("s")
    ids = torch.tensor([[1, 5, 9, 22, 7]])

    got = run_chain(model, stages, "s", ids)

    torch.testing.assert_close(got, model(ids).logits, atol=1e-5, rtol=1e-5)


def test_stage_loads_only_its_layers(reference):
    _, path = reference
    stage = load_stage(path, 2, 3, dtype=torch.float32)

    assert stage.config.num_hidden_layers == 2
    assert len(stage.module.layers) == 2
    assert stage.module.embed_tokens is None
    assert all(p.device.type != "meta" for p in stage.module.parameters())


def test_missing_layer_weight_fails_loudly(reference, tmp_path):
    import json
    import shutil

    _, path = reference
    broken = tmp_path / "broken"
    shutil.copytree(path, broken)
    index_path = broken / "model.safetensors.index.json"
    index = json.loads(index_path.read_text())
    del index["weight_map"]["model.layers.2.mlp.up_proj.weight"]
    index_path.write_text(json.dumps(index))

    with pytest.raises(stage_engine.CheckpointError, match="layers.0.mlp.up_proj.weight"):
        load_stage(str(broken), 2, 3, dtype=torch.float32)


@torch.no_grad()
def test_incremental_decode_matches_unsplit_model(reference):
    model, path = reference
    stages = two_stages(path)
    for stage in stages:
        stage.open_session("s")
    prompt = torch.tensor([[1, 5, 9, 22, 7]])
    run_chain(model, stages, "s", prompt)

    next_tokens = [33, 2, 64]
    full = prompt
    for token in next_tokens:
        full = torch.cat([full, torch.tensor([[token]])], dim=1)
        got = run_chain(model, stages, "s", torch.tensor([[token]]))[:, -1]
        torch.testing.assert_close(got, model(full).logits[:, -1], atol=1e-5, rtol=1e-5)

    assert stages[0].session_length("s") == prompt.shape[1] + len(next_tokens)


@torch.no_grad()
def test_sessions_keep_separate_caches(reference):
    model, path = reference
    stages = two_stages(path)
    for stage in stages:
        stage.open_session("a")
        stage.open_session("b")

    run_chain(model, stages, "a", torch.tensor([[1, 2, 3, 4]]))
    got_b = run_chain(model, stages, "b", torch.tensor([[9, 8]]))

    torch.testing.assert_close(got_b, model(torch.tensor([[9, 8]])).logits, atol=1e-5, rtol=1e-5)
    assert stages[0].session_length("a") == 4
    assert stages[0].session_length("b") == 2


def test_freed_session_is_unknown(reference):
    _, path = reference
    stage = load_stage(path, 0, 1, dtype=torch.float32)
    stage.open_session("s")
    stage.free_session("s")

    with pytest.raises(stage_engine.UnknownSessionError):
        stage.forward("s", torch.zeros(1, 1, 64))
    with pytest.raises(stage_engine.UnknownSessionError):
        stage.free_session("s")


def test_forward_on_never_opened_session_is_unknown(reference):
    _, path = reference
    stage = load_stage(path, 0, 1, dtype=torch.float32)

    with pytest.raises(stage_engine.UnknownSessionError):
        stage.forward("never-opened", torch.zeros(1, 1, 64))


@pytest.mark.parametrize(
    "bad, reason",
    [
        (torch.zeros(1, 64), "rank"),
        (torch.zeros(2, 3, 64), "batch"),
        (torch.zeros(1, 0, 64), "empty"),
        (torch.zeros(1, 3, 63), "hidden size"),
        (torch.zeros(1, 3, 64, dtype=torch.float16), "dtype"),
    ],
)
def test_malformed_hidden_states_are_rejected(reference, bad, reason):
    _, path = reference
    stage = load_stage(path, 0, 1, dtype=torch.float32)
    stage.open_session("s")

    with pytest.raises(stage_engine.StageInputError, match=reason):
        stage.forward("s", bad)
    assert stage.session_length("s") == 0  # cache untouched


def test_pass_beyond_context_window_is_rejected(reference):
    _, path = reference
    stage = load_stage(path, 0, 1, dtype=torch.float32)
    stage.open_session("s")
    stage.forward("s", torch.zeros(1, 30, 64))

    with pytest.raises(stage_engine.StageInputError, match="context window"):
        stage.forward("s", torch.zeros(1, 3, 64))  # 30 + 3 > 32
    assert stage.session_length("s") == 30


def fake_hub(reference_dir, requested):
    """Stands in for hf_hub_download: serves files from the local tiny
    checkpoint and records every filename asked for."""
    import os

    def fetch(repo_id, filename):
        requested.append(filename)
        return os.path.join(reference_dir, filename)

    return fetch


def test_fetch_downloads_only_this_stages_shards(reference):
    import json
    import os

    _, path = reference
    requested = []
    weight_map = json.load(open(os.path.join(path, "model.safetensors.index.json")))["weight_map"]
    wanted = {s for n, s in weight_map.items() if n.startswith(("model.layers.2.", "model.layers.3."))}
    unwanted = set(weight_map.values()) - wanted

    local = stage_engine.fetch_checkpoint("org/tiny", 2, 3, fetch=fake_hub(path, requested))
    stage = load_stage(local, 2, 3, dtype=torch.float32)

    assert {"config.json", "model.safetensors.index.json"} <= set(requested)
    assert wanted <= set(requested)
    assert not (unwanted & set(requested))
    assert stage.config.num_hidden_layers == 2


def test_fetch_passes_a_local_directory_through(reference):
    _, path = reference

    def no_fetch(repo_id, filename):
        pytest.fail("a local directory must not hit the Hub")

    assert stage_engine.fetch_checkpoint(path, 0, 1, fetch=no_fetch) == path


def test_fetch_falls_back_to_single_file_when_no_index(tmp_path):
    from huggingface_hub.errors import EntryNotFoundError

    requested = []

    def fetch(repo_id, filename):
        requested.append(filename)
        if filename == "model.safetensors.index.json":
            raise EntryNotFoundError("no index")
        return str(tmp_path / filename)

    local = stage_engine.fetch_checkpoint("org/tiny", 0, 1, fetch=fetch)

    assert "model.safetensors" in requested
    assert local == str(tmp_path)


def test_fetch_propagates_non_not_found_errors(tmp_path):
    requested = []

    def fetch(repo_id, filename):
        requested.append(filename)
        if filename == "model.safetensors.index.json":
            raise RuntimeError("network down")
        return str(tmp_path / filename)

    with pytest.raises(RuntimeError, match="network down"):
        stage_engine.fetch_checkpoint("org/tiny", 0, 1, fetch=fetch)
    assert "model.safetensors" not in requested
