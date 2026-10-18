# Issue #78 — Stage Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A networking-free stage engine that loads only layers `first..last` of a Qwen2 checkpoint and runs `forward(session_id, hidden_states) -> hidden_states` with one KV cache per session.

**Architecture:** Build a transformers `Qwen2Model` from a *stage config* (layer count and `layer_types` sliced to the range) on the meta device. Drop its embedding, replace its final norm with identity (the client applies embedding, final norm and LM head), and rebuild its rotary embedding off the meta device. Then load only the checkpoint tensors named `model.layers.{first..last}.*`, renamed to local indices `layers.{0..n-1}.*`. Sessions are explicit: `open_session` creates a `DynamicCache`, `forward` on an unknown session is an error, and `free_session` drops it.

**Tech Stack:** Python 3.11, torch 2.11.0, transformers 5.15.0, safetensors 0.8.0, huggingface-hub 1.27.0, pytest.

Issue: [#78](https://github.com/Zenkai-Dynamics/Mycelium/issues/78) · Parent: [#77](https://github.com/Zenkai-Dynamics/Mycelium/issues/77) · Spec: [Phase 3 design](../specs/2026-10-18-phase-3-model-parallelism-design.md)

## Global Constraints

- Pins: `torch==2.11.0`, `transformers==5.15.0`, `safetensors==0.8.0`, `huggingface-hub==1.27.0`. These are the exact versions `uv.lock` already resolves through `vllm`, so a node gets no new versions.
- New optional extra `stage` in `pyproject.toml`. It is **not** added to `node`. Unlike `node`, it must install on macOS (`uv pip install -e ".[stage]"`).
- No networking in this ticket. No coordinator, websocket or CLI changes.
- The embedding layer, final norm and LM head are never loaded by a stage.
- Batch size is exactly 1 (single-stream; batching is a Phase 3 non-goal).
- Default dtype `torch.bfloat16`. Tests use `torch.float32` on CPU.
- Tests use a tiny random-weight Qwen2 checkpoint saved to `tmp_path`. No network and no GPU.
- Stage tests skip cleanly when torch isn't installed (`pytest.importorskip`), so a base-only install still runs the rest of the suite.

## Decisions made while planning

Recorded here rather than in a separate design doc, because the Phase 3 spec already covers this ticket's *why*:

- **Final RMSNorm goes to the client, with the LM head.** Prototyped: with norm, embedding and head applied outside the stages, two chained stages reproduce the unsplit model's prefill logits exactly (max diff 0.0) and decode within 1.2e-7 in float32.
- **Sessions are explicit, not created on first use.** If a stage silently created a cache for an unknown session, a stage that lost its state would compute against an empty cache and return confidently wrong activations. `forward` on an unknown session raises `UnknownSessionError`, which #80 maps to a client fault.
- **A `stage` extra, not a `node` dependency.** The `node` extra is Linux-only (vLLM). The stage engine's tests must run on a developer's Mac, as the rest of the suite does.
- **Selective download.** A Qwen2.5-72B volunteer must download only the shards holding its layers (~36 GB), not all ~145 GB. The fetch function is injectable for tests, matching the codebase's existing injection pattern (`identity_verifier`, `random_source`).
- Rollback for speculative decoding (`DynamicCache.crop`) is confirmed to work in transformers 5.15 but belongs to #82. Not exposed here.

## File Structure

- Create `src/mycelium/node/stage_engine.py`: the whole engine. Loading, sessions, validation, and checkpoint resolution are one cohesive unit (~150 lines).
- Create `tests/node/test_stage_engine.py`: equivalence, sessions, validation, and selective-download tests.
- Modify `pyproject.toml`: add the `stage` extra.
- Modify `docs/dependencies.md`: a pinned-versions table for `mycelium[stage]`.
- Modify `docs/SETUP.md`: one paragraph on installing `.[stage]` to run the stage engine's tests.

---

### Task 1: Load a layer range and match the unsplit model on prefill

**Files:**
- Modify: `pyproject.toml` (the `[project.optional-dependencies]` table)
- Modify: `docs/dependencies.md` (new section after "Pinned versions (`mycelium` base / `mycelium[coordinator]`)")
- Modify: `docs/SETUP.md` (after step 2's plain-pip block)
- Create: `src/mycelium/node/stage_engine.py`
- Create: `tests/node/test_stage_engine.py`

**Interfaces:**
- Produces: `load_stage(checkpoint_dir: str, first: int, last: int, *, dtype=torch.bfloat16, device="cpu") -> StageEngine`, and `StageEngine` with attributes `first: int`, `last: int`, `config` (the stage's sliced `Qwen2Config`), and `module` (the underlying `Qwen2Model`).

- [ ] **Step 1: Add the `stage` extra and install it**

In `pyproject.toml`, add under `[project.optional-dependencies]` after `client = []`:

```toml
# Phase 3 stage node (issue #78). Same versions uv.lock already resolves
# through vllm; kept out of `node` so it installs on macOS for tests.
stage = [
    "torch==2.11.0",
    "transformers==5.15.0",
    "safetensors==0.8.0",
    "huggingface-hub==1.27.0",
]
```

Run: `uv pip install -e ".[stage]"`
Expected: installs torch 2.11.0 and transformers 5.15.0. Then `.venv/bin/python -c "import torch, transformers; print(torch.__version__, transformers.__version__)"` prints `2.11.0 5.15.0`.

- [ ] **Step 2: Write the failing equivalence test**

Create `tests/node/test_stage_engine.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'mycelium.node.stage_engine'`.

- [ ] **Step 4: Write the loader**

Create `src/mycelium/node/stage_engine.py`:

```python
"""Serves one contiguous range of a Qwen2 model's decoder layers, with one
KV cache per session.

See the Phase 3 design doc and issue #78. No networking: the stage node
(#80/#81) wraps this. The embedding layer, final norm and LM head are never
loaded here — the client holds them, so a stage only ever sees hidden
states, never token IDs or logits.
"""

from __future__ import annotations

import copy
import json
import os

import torch
from safetensors import safe_open
from torch import nn
from transformers import DynamicCache, Qwen2Config
from transformers.models.qwen2.modeling_qwen2 import Qwen2Model, Qwen2RotaryEmbedding

INDEX_FILE = "model.safetensors.index.json"
SINGLE_FILE = "model.safetensors"
LAYER_PREFIX = "model.layers."


class StageEngine:
    """Layers `first..last` (inclusive) of one model, plus per-session KV caches."""

    def __init__(self, module: Qwen2Model, config: Qwen2Config, first: int, last: int):
        self.module = module
        self.config = config
        self.first = first
        self.last = last
        self._sessions: dict[str, DynamicCache] = {}

    def open_session(self, session_id: str) -> None:
        self._sessions[session_id] = DynamicCache(config=self.config)

    @torch.no_grad()
    def forward(self, session_id: str, hidden_states: torch.Tensor) -> torch.Tensor:
        cache = self._sessions[session_id]
        out = self.module(inputs_embeds=hidden_states, past_key_values=cache, use_cache=True)
        return out.last_hidden_state


def _stage_config(config: Qwen2Config, first: int, last: int) -> Qwen2Config:
    stage = copy.deepcopy(config)
    stage.num_hidden_layers = last - first + 1
    stage.layer_types = config.layer_types[first : last + 1]
    return stage


def _weight_map(checkpoint_dir: str) -> dict[str, str]:
    """Tensor name -> shard filename, for sharded and single-file checkpoints."""
    index = os.path.join(checkpoint_dir, INDEX_FILE)
    if os.path.exists(index):
        with open(index) as f:
            return json.load(f)["weight_map"]
    with safe_open(os.path.join(checkpoint_dir, SINGLE_FILE), "pt") as f:
        return {name: SINGLE_FILE for name in f.keys()}


def _layer_of(name: str) -> tuple[int, str] | None:
    """`model.layers.12.mlp.up_proj.weight` -> (12, 'mlp.up_proj.weight')."""
    if not name.startswith(LAYER_PREFIX):
        return None
    index, rest = name[len(LAYER_PREFIX) :].split(".", 1)
    return int(index), rest


def _needed_tensors(weight_map: dict[str, str], first: int, last: int) -> dict[str, str]:
    """Checkpoint tensor name -> shard, for this stage's layers only."""
    needed = {}
    for name, shard in weight_map.items():
        layer = _layer_of(name)
        if layer is not None and first <= layer[0] <= last:
            needed[name] = shard
    return needed


def load_stage(
    checkpoint_dir: str,
    first: int,
    last: int,
    *,
    dtype: torch.dtype = torch.bfloat16,
    device: str = "cpu",
) -> StageEngine:
    """Load layers `first..last` of the Qwen2 checkpoint in `checkpoint_dir`.

    The model is built on the meta device so no layer outside the range is
    ever materialized; only this stage's tensors are read from disk.
    """
    config = Qwen2Config.from_pretrained(checkpoint_dir)
    stage_config = _stage_config(config, first, last)
    with torch.device("meta"):
        module = Qwen2Model(stage_config)
    module.embed_tokens = None
    module.norm = nn.Identity()
    # Its inv_freq buffer is computed in __init__, so it must be built off meta.
    module.rotary_emb = Qwen2RotaryEmbedding(config=stage_config).to(device)

    state = {}
    needed = _needed_tensors(_weight_map(checkpoint_dir), first, last)
    for shard in sorted(set(needed.values())):
        with safe_open(os.path.join(checkpoint_dir, shard), "pt", device=device) as f:
            for name in (n for n, s in needed.items() if s == shard):
                index, rest = _layer_of(name)
                state[f"layers.{index - first}.{rest}"] = f.get_tensor(name).to(dtype)
    module.load_state_dict(state, strict=False, assign=True)
    module.eval()
    return StageEngine(module, stage_config, first, last)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: 2 passed.

(Don't pass `device=` to `Qwen2RotaryEmbedding`. It is deprecated in transformers 5.15 and removed in 5.18, hence `.to(device)`.) If `test_stage_loads_only_its_layers` reports a meta parameter, a checkpoint tensor wasn't renamed correctly. Print `load_state_dict`'s `missing_keys`.

- [ ] **Step 6: Make a silently missing tensor impossible**

`strict=False` is needed because the rotary buffer isn't in the checkpoint, but it would also hide a missing layer weight. Write the failing test first, appending to `tests/node/test_stage_engine.py`:

```python
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
```

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py::test_missing_layer_weight_fails_loudly -v`
Expected: FAIL, `AttributeError: module 'mycelium.node.stage_engine' has no attribute 'CheckpointError'`.

Then in `stage_engine.py`, add after the constants:

```python
class CheckpointError(Exception):
    """The checkpoint doesn't contain every tensor this stage's layers need."""
```

and replace the `module.load_state_dict(...)` line with:

```python
    result = module.load_state_dict(state, strict=False, assign=True)
    missing = [k for k in result.missing_keys if not k.startswith("rotary_emb.")]
    if missing or result.unexpected_keys:
        raise CheckpointError(f"missing {missing}, unexpected {result.unexpected_keys}")
```

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: 3 passed.

- [ ] **Step 7: Document the extra**

In `docs/dependencies.md`, add this section after the base/coordinator pinned-versions section:

```markdown
## Pinned versions (`mycelium[stage]`)

Phase 3's stage engine (issue #78) runs a contiguous range of a model's
layers on HF transformers, not vLLM — see the
[Phase 3 design doc](superpowers/specs/2026-10-18-phase-3-model-parallelism-design.md).

| Package | Version | Why this exact version |
|---|---|---|
| `torch` | 2.11.0 | The version `uv.lock` already resolves through `vllm[audio]==0.25.1`. |
| `transformers` | 5.15.0 | Same — already resolved through vLLM; the stage engine depends on its Qwen2 internals (`Qwen2Model`, `Qwen2RotaryEmbedding`, `DynamicCache`). |
| `safetensors` | 0.8.0 | Same — reads only the checkpoint tensors a stage needs. |
| `huggingface-hub` | 1.27.0 | Same — downloads only the shards holding a stage's layers. |

Deliberately a separate extra rather than part of `node`: `node` is
Linux-only (vLLM), while the stage engine's tests run on any platform.
```

In `docs/SETUP.md`, add after step 2's plain-pip code block:

```markdown
   To run the Phase 3 stage engine's tests (`tests/node/test_stage_engine.py`),
   also install the `stage` extra — CPU-only torch is enough, no GPU needed:

   ```bash
   uv pip install -e ".[stage]"
   ```

   Without it those tests are skipped, not failed.
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml docs/dependencies.md docs/SETUP.md src/mycelium/node/stage_engine.py tests/node/test_stage_engine.py
git commit -m "feat: stage engine loads one layer range of a Qwen2 checkpoint (#78)"
```

---

### Task 2: Per-session KV caches and incremental decode

**Files:**
- Modify: `src/mycelium/node/stage_engine.py`
- Test: `tests/node/test_stage_engine.py`

**Interfaces:**
- Consumes: `load_stage`, `StageEngine.open_session`, `StageEngine.forward` from Task 1.
- Produces: `StageEngine.free_session(session_id: str) -> None`, `StageEngine.session_length(session_id: str) -> int` (tokens cached), and `UnknownSessionError(KeyError)`, raised by `forward`, `free_session` and `session_length` for a session that isn't open. #80 maps it to a client fault and #82 builds rollback on these caches.

- [ ] **Step 1: Write the failing tests**

Append to `tests/node/test_stage_engine.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: the four new tests FAIL. `session_length` and `free_session` raise `AttributeError`, and `stage_engine.UnknownSessionError` doesn't exist. Task 1's three tests still pass.

- [ ] **Step 3: Implement sessions**

In `stage_engine.py`, add after `CheckpointError`:

```python
class UnknownSessionError(KeyError):
    """No open session with this id. A stage never creates a cache on first
    use: a stage that lost its state must fail, not compute against an
    empty cache and return confidently wrong activations."""
```

Replace `StageEngine.forward` and add the new methods:

```python
    def _cache(self, session_id: str) -> DynamicCache:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise UnknownSessionError(session_id) from None

    def free_session(self, session_id: str) -> None:
        self._cache(session_id)
        del self._sessions[session_id]

    def session_length(self, session_id: str) -> int:
        return self._cache(session_id).get_seq_length()

    @torch.no_grad()
    def forward(self, session_id: str, hidden_states: torch.Tensor) -> torch.Tensor:
        cache = self._cache(session_id)
        out = self.module(inputs_embeds=hidden_states, past_key_values=cache, use_cache=True)
        return out.last_hidden_state
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/stage_engine.py tests/node/test_stage_engine.py
git commit -m "feat: stage engine keeps one KV cache per explicit session (#78)"
```

---

### Task 3: Reject malformed hidden states as a client fault

**Files:**
- Modify: `src/mycelium/node/stage_engine.py`
- Test: `tests/node/test_stage_engine.py`

**Interfaces:**
- Consumes: `StageEngine.forward` and `session_length` from Task 2.
- Produces: `StageInputError(ValueError)`, raised by `forward` *before* touching the cache, for a wrong rank, batch ≠ 1, an empty sequence, a wrong hidden size, a wrong dtype, or a pass that would exceed `max_position_embeddings`. #80 maps it to a client fault, so no reputation damage.

- [ ] **Step 1: Write the failing tests**

Append to `tests/node/test_stage_engine.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: the six new cases FAIL with `AttributeError: ... no attribute 'StageInputError'`.

- [ ] **Step 3: Implement validation**

In `stage_engine.py`, add after `UnknownSessionError`:

```python
class StageInputError(ValueError):
    """Hidden states this stage can't accept. A client fault (#58): the
    stage served the request correctly and the input was wrong."""
```

Add the `_dtype` attribute in `StageEngine.__init__` (after `self.last = last`):

```python
        self._dtype = next(module.parameters()).dtype
```

Add this method to `StageEngine` and call it at the top of `forward`, after `cache = self._cache(session_id)`, as `self._check(cache, hidden_states)`:

```python
    def _check(self, cache: DynamicCache, hidden: torch.Tensor) -> None:
        if hidden.dim() != 3:
            raise StageInputError(f"expected rank 3 [1, seq, hidden], got rank {hidden.dim()}")
        batch, seq, size = hidden.shape
        if batch != 1:
            raise StageInputError(f"batch must be 1, got {batch}")
        if seq == 0:
            raise StageInputError("empty sequence")
        if size != self.config.hidden_size:
            raise StageInputError(f"hidden size must be {self.config.hidden_size}, got {size}")
        if hidden.dtype != self._dtype:
            raise StageInputError(f"dtype must be {self._dtype}, got {hidden.dtype}")
        limit = self.config.max_position_embeddings
        if cache.get_seq_length() + seq > limit:
            raise StageInputError(
                f"{cache.get_seq_length()} cached + {seq} new tokens exceeds the "
                f"context window of {limit}"
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/stage_engine.py tests/node/test_stage_engine.py
git commit -m "feat: stage engine rejects malformed hidden states as a client fault (#78)"
```

---

### Task 4: Fetch only the shards a stage needs from the Hub

**Files:**
- Modify: `src/mycelium/node/stage_engine.py`
- Test: `tests/node/test_stage_engine.py`

**Interfaces:**
- Consumes: `load_stage`, `_weight_map`, `_needed_tensors`, `INDEX_FILE`, `SINGLE_FILE` from Task 1.
- Produces: `fetch_checkpoint(model: str, first: int, last: int, *, fetch=hf_hub_download) -> str`. It returns a local directory holding `config.json` and exactly the files this stage reads. A local directory passes through unchanged. `fetch(repo_id, filename) -> local_path` is injectable for tests. #81 calls `load_stage(fetch_checkpoint(model, first, last), first, last, ...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/node/test_stage_engine.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: the two new tests FAIL with `AttributeError: ... no attribute 'fetch_checkpoint'`.

- [ ] **Step 3: Implement selective fetch**

`hf_hub_download` places every file of one repo revision in the same snapshot directory, so the parent directory of `config.json` is a valid `checkpoint_dir` once the needed shards are downloaded too. The fake in the test mimics this by serving every file from one directory.

In `stage_engine.py`, add to the imports:

```python
from huggingface_hub import hf_hub_download
```

and add after `_needed_tensors`:

```python
def fetch_checkpoint(model: str, first: int, last: int, *, fetch=hf_hub_download) -> str:
    """Local directory holding what `load_stage(…, first, last)` reads.

    A Hub model downloads only `config.json`, the shard index, and the
    shards containing layers `first..last` — about a quarter of Qwen2.5-72B
    for a four-stage chain, instead of all of it. `fetch` is injectable for
    tests only; production callers take the real downloader.
    """
    if os.path.isdir(model):
        return model
    checkpoint_dir = os.path.dirname(fetch(model, "config.json"))
    try:
        fetch(model, INDEX_FILE)
    except Exception:
        # Small models ship one unsharded file and no index.
        fetch(model, SINGLE_FILE)
        return checkpoint_dir
    for shard in sorted(set(_needed_tensors(_weight_map(checkpoint_dir), first, last).values())):
        fetch(model, shard)
    return checkpoint_dir
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/node/test_stage_engine.py -v`
Expected: 15 passed.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass (370 existing plus 15 new). No existing test changes.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/node/stage_engine.py tests/node/test_stage_engine.py
git commit -m "feat: fetch only the shards holding a stage's layers (#78)"
```

---

## Acceptance criteria → tasks

| Issue #78 criterion | Covered by |
|---|---|
| Loads only the declared layer range; other layers never materialized | Task 1 (`test_stage_loads_only_its_layers`, meta-device build, `CheckpointError`), Task 4 (unneeded shards never fetched) |
| Chained stages match an unsplit model, prefill and incremental decode | Task 1 (prefill), Task 2 (decode) |
| Separate sessions, separate caches; freeing releases | Task 2 |
| Shape mismatch raises a distinct client-fault error | Task 3 (`StageInputError`); unknown session is Task 2 (`UnknownSessionError`) |
