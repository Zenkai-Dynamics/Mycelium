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


class CheckpointError(Exception):
    """The checkpoint doesn't contain every tensor this stage's layers need."""


class UnknownSessionError(KeyError):
    """No open session with this id. A stage never creates a cache on first
    use: a stage that lost its state must fail, not compute against an
    empty cache and return confidently wrong activations."""


class StageInputError(ValueError):
    """Hidden states this stage can't accept. A client fault (#58): the
    stage served the request correctly and the input was wrong."""


class StageEngine:
    """Layers `first..last` (inclusive) of one model, plus per-session KV caches."""

    def __init__(self, module: Qwen2Model, config: Qwen2Config, first: int, last: int):
        self.module = module
        self.config = config
        self.first = first
        self.last = last
        self._dtype = next(module.parameters()).dtype
        self._sessions: dict[str, DynamicCache] = {}

    def open_session(self, session_id: str) -> None:
        self._sessions[session_id] = DynamicCache(config=self.config)

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

    @torch.no_grad()
    def forward(self, session_id: str, hidden_states: torch.Tensor) -> torch.Tensor:
        cache = self._cache(session_id)
        self._check(cache, hidden_states)
        out =self.module(inputs_embeds=hidden_states, past_key_values=cache, use_cache=True)
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
    result = module.load_state_dict(state, strict=False, assign=True)
    missing = [k for k in result.missing_keys if not k.startswith("rotary_emb.")]
    if missing or result.unexpected_keys:
        raise CheckpointError(f"missing {missing}, unexpected {result.unexpected_keys}")
    module.eval()
    return StageEngine(module, stage_config, first, last)
