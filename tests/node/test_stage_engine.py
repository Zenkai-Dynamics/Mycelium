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
