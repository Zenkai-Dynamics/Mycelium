# Issue #6 — Validate the Phase 0 Model via vLLM on Target Hardware — Design

Date: 2026-08-14
Status: Approved, not yet implemented
Issue: [#6 — Validate the Phase 0 model runs via vLLM on target hardware](https://github.com/Zenkai-Dynamics/Mycelium/issues/6)

This is the condensed record of the decisions made while brainstorming/
grilling issue #6, before implementation starts. It exists so the
*reasoning* behind the model choice isn't lost, per the pattern
established in the [issue #1](2026-08-12-issue-1-project-skeleton-design.md),
[issue #2](2026-08-14-issue-2-dependency-pinning-design.md),
[issue #3](2026-08-14-issue-3-developer-setup-guide-design.md),
[issue #4](2026-08-14-issue-4-node-network-reachability-design.md), and
[issue #5](2026-08-14-issue-5-coordinator-node-transport-design.md)
design docs.

## What issue #6 asks for

Pick the specific open-weight HF model Phase 0 will serve, sized to fit
comfortably on the smallest target node's VRAM, and confirm it runs
correctly under vLLM directly on real target hardware — no node-agent
code involved yet. Acceptance criteria: the model choice is documented in
`docs/phases/phase-0-foundation.md` (replacing its "TBD" note) along with
why it fits; `vllm serve <model>` (or equivalent) runs successfully on a
real target GPU machine; a prompt sent directly to that raw vLLM server
returns a correct completion.

## Real hardware facts gathered

- **`a6000`**: 4× NVIDIA RTX A6000, 48.5 GB VRAM each (`nvidia-smi
  --query-gpu=memory.total`), fully accessible over the university VPN.
  Root disk is tight — 95% full, only 51 GB free (`df -h /`) — but
  `/mnt/disk1` has 269 GB free and is already used by several other
  users/projects on this shared machine (`docker`, `Framework`,
  `geetika`, `LLMforCode`, `PeptideData`, `slakshna` directories present).
- **`h100`**: 2× H100 NVL (confirmed via `/proc/driver/nvidia/gpus/*/information`,
  since `nvidia-smi` itself fails with "Insufficient Permissions"). Real,
  currently-unresolved blocker: the `bapi` user is not a member of the
  `gpuaccess` group that owns `/dev/nvidia0`/`/dev/nvidiactl`
  (`crw-rw---- root gpuaccess`) — confirmed by running
  `torch.cuda.is_available()` inside an existing, clearly GPU-purposed
  conda environment (`h100bench`, torch 2.11.0+cu126) already present on
  the machine, which returned `False`. This is an OS-level permission gap
  outside this session's control — it needs whoever has root on that
  machine to add `bapi` to `gpuaccess`. `h100` does already have several
  instruct models cached under `bapi`'s HF cache, including a fully
  downloaded `Qwen2.5-7B-Instruct` (266 GB cache total) — a strong signal
  this was a deliberate prior setup, once GPU access is restored.
- **`paramrudra`**: has a real `gpu` SLURM partition (30 nodes, 2 GPUs
  each per `sinfo`), but GPU model/VRAM is unconfirmed — would need a
  live SLURM allocation to check, the same operator-time tradeoff already
  declined once in [issue #5](2026-08-14-issue-5-coordinator-node-transport-design.md).
- Checked candidate open-weight instruct models directly against the HF
  API (not assumed): `Qwen/Qwen2.5-7B-Instruct` (ungated, 12.1M
  downloads/30d, 4 safetensors shards), `Qwen/Qwen3-8B` (ungated, 16.3M
  downloads/30d, 5 shards), `Qwen/Qwen3-4B-Instruct-2507` (ungated, 3.2M
  downloads/30d, 3 shards), `mistralai/Mistral-7B-Instruct-v0.3` (ungated,
  4.1M downloads/30d, 4 shards). `meta-llama/Llama-3.1-8B-Instruct` is
  gated (`manual` approval required) — ruled out to avoid an
  HF-token/approval dependency for this issue.

## Decisions made

**Node: `a6000` only for this issue.** `h100`'s permission gap is a real
blocker the operator can't fix from within this session — rather than
stall the issue on it, `a6000` (already fully accessible, already the
node most of this project's real-hardware verification has used since
issue #2) carries this issue's validation alone. `h100`'s fix is
explicitly out of scope here, left for the operator to resolve
out-of-band whenever convenient.

**Sizing target: a single GPU (48 GB budget), not tensor-parallel across
`a6000`'s 4 cards.** `docs/phases/phase-0-foundation.md`'s existing "fit
comfortably on the smallest node's VRAM" language is read conservatively
here: a future smaller or single-GPU node should still be able to run
whatever model gets chosen. Tensor parallelism across multiple GPUs adds
Ray/vLLM orchestration complexity ([issue #7](https://github.com/Zenkai-Dynamics/Mycelium/issues/7)'s
job to wire up, not this issue's), and nothing about Phase 0's scope
needs a larger-than-single-GPU model yet. `CUDA_VISIBLE_DEVICES=0` pins
the served instance to exactly one card, proving single-GPU fit rather
than accidentally spreading across all four by default.

**Model: `Qwen/Qwen2.5-7B-Instruct`.** Rejected `Qwen3-8B` (newer
generation, more exposure to unproven quirks against this project's
already-pinned `vllm==0.25.1`) and `Mistral-7B-Instruct-v0.3` (less
vLLM-specific community mileage than the Qwen options) in favor of the
most vLLM-tested, most-downloaded, ungated option — lowest risk of
hitting an unexpected compatibility issue during this validation. At
~15 GB in bf16, it fits in a 48 GB card with large headroom for KV cache
and concurrent requests.

**Serving mechanism: plain `vllm serve` CLI, not `ray.serve.llm`.**
Issue #6 explicitly says "no node-agent code involved yet" — the
Ray-orchestrated wrapper `ray[llm]` was pinned for
([issue #2](2026-08-14-issue-2-dependency-pinning-design.md)'s design)
is [issue #7](https://github.com/Zenkai-Dynamics/Mycelium/issues/7)'s
job to wire into the node agent. This issue only needs to prove vLLM
itself works on real hardware, using the project's already-pinned `node`
extra (`uv sync --extra node`, per
[docs/dependencies.md](../../dependencies.md)) so the exact pinned stack
gets validated, not some other vLLM version installed ad hoc.

**Model storage: `HF_HOME` redirected to `/mnt/disk1/Framework/mycelium-hf-cache`.**
`a6000`'s root disk is already at 95% full (51 GB free) and is a machine
shared with several other users/projects — downloading ~15 GB of model
weights there would work today but leaves little margin and tightens a
disk other people also depend on. `/mnt/disk1` itself is `root`-owned
(`drwxr-xr-x`) and not writable by the operator's account directly — but
`/mnt/disk1/Framework` (world-writable, and clearly the operator's own
prior working directory: it contains `Bhaskera.txt`, the same prior
framework named in `docs/phases/phase-0-foundation.md`'s architecture
section) already exists and is writable, so the cache lives at
`/mnt/disk1/Framework/mycelium-hf-cache` rather than needing `sudo` for a
new top-level directory.

**Verification: the OpenAI-compatible `/v1/chat/completions` endpoint,**
not the raw `/v1/completions` endpoint — matches how an Instruct-tuned
model is actually meant to be used, and is the interface a real client
would eventually talk to.

**No new ADR.** Unlike [ADR-0002](../../adr/0002-node-transport-model.md)
(issue #4), this is a parameter/spec choice (which model, sized how)
rather than an architectural decision — issue #6's acceptance criteria
only asks for the `docs/phases/phase-0-foundation.md` update, and that's
sufficient here.

## Live verification & debugging

Running `vllm serve Qwen/Qwen2.5-7B-Instruct` for real on `a6000` did not
work on the first attempt — it took real root-cause debugging (via the
systematic-debugging process: read the error, form a hypothesis, test it
minimally, verify or move to a new hypothesis) across two distinct bugs
before a prompt actually returned a completion. This section is the
record, so the fixes aren't lost and don't need rediscovering.

**Attempt 1 — system nvcc, wrong CUDA generation entirely.** The `node`
extra installed cleanly (`uv sync --extra node`, matching issue #2's
already-pinned `ray[llm]==2.57.0`/`vllm[audio]==0.25.1`). `vllm serve`
started, but failed while JIT-compiling a `flashinfer` sampling kernel:

```
error: class "cub::_V_300302_SM_860::BlockAdjacentDifference<__nv_bool, 512, 1, 1>" has no member "FlagHeads"
```

The compile command showed `nvcc` resolving to `/usr/bin/nvcc` — the
machine's system-wide CUDA toolkit, version **12.0** (from January
2023) — while the project's pinned `torch==2.11.0` needs CUDA **13**
(`torch.version.cuda == "13.0"`, already documented in
`docs/dependencies.md`). The venv itself has its own bundled CUDA 13
`nvcc` (`nvidia-cuda-nvcc` pip package), never on `PATH` by default.

**Attempt 2 — venv's own nvcc, but internally mismatched.** Forcing
`PATH`/`CUDA_HOME` to the venv's own CUDA 13 `nvcc` got much further —
the model loaded, CUDA graphs captured successfully, the server started —
but crashed on the *first real sampling call* with a different error:

```
error: "CUDA compiler and CUDA toolkit headers are incompatible, please check your include paths"
```

Root-caused precisely: the venv's `nvcc` reports itself as version
**13.2** (`__CUDACC_VER_MAJOR__.MINOR__`), but the `cuda_runtime_api.h`
it resolves against (from the separately-versioned `nvidia-cuda-runtime`
pip package) defines `CUDART_VERSION 13000` (13.0) — two different
NVIDIA CUDA sub-packages, resolved independently by `uv`'s resolver with
nothing forcing them to share a release line. **This drift existed in
the project's own committed `uv.lock`**, not just this manual PATH
experiment: `nvidia-cuda-nvcc` was pinned to `13.2.86` (pulled in via
`cuda-tile`'s `tileiras` extra, itself a transitive dependency with no
version ceiling tying it to the rest of the CUDA 13.0.x stack), while
`nvidia-cuda-runtime`/`nvidia-cuda-nvrtc` sat at `13.0.9x`/`13.0.88`.
**Fix:** added `[tool.uv].constraint-dependencies =
["nvidia-cuda-nvcc==13.0.88"]` to `pyproject.toml` — matching
`nvidia-cuda-nvrtc`'s already-pinned `13.0.88` — and regenerated
`uv.lock`/`requirements-node-lock.txt`. This is a genuine, permanent
correction to the dependency stack pinned in issue #2, not scoped to
this issue alone; anyone installing `mycelium[node]` fresh would have
hit the same `FlagHeads` JIT-compile failure the moment `vllm serve`
actually tried to sample a token (issue #2's own verification never
exercised this path — it only checked `import ray, vllm` and
`ray.serve.llm`, not a real serve+sample call).

**Attempt 3 — aligned toolchain, same `FlagHeads` error resurfaces.**
nvcc and the runtime headers it version-checks against
(`nvidia-cuda-runtime`/`nvidia-cuda-nvrtc`) are now aligned to 13.0.x —
`nvidia-cuda-crt`, `nvidia-nvvm`, and `nvidia-cuda-cccl` remain
resolver-chosen on their own release lines and aren't constrained by this
fix; it works today but isn't a guarantee against the same class of drift
recurring for a different sub-package pair on a future `uv lock` refresh.
With no manual `PATH` override needed, the *original* `FlagHeads` error
came back, unchanged. This ruled out "CUDA toolkit version" as the cause
of `FlagHeads` specifically — the error is independent of which
internally-consistent CUDA generation compiles it. Traced further:
`flashinfer`'s own bundled CCCL/`cub` copy
(`flashinfer/data/cccl/cub`, `CUB_VERSION 300302` — matching the exact
`_V_300302_SM_860` namespace in the error) genuinely *does* define
`FlagHeads` on `BlockAdjacentDifference` at the source level, confirmed
by reading the header directly — yet the compiler still reports it
missing. The CUDA 13 toolkit package (`nvidia-cuda-nvcc`) also ships its
own bundled CCCL copy at a different, competing location
(`nvidia/cu13/include/cccl/cub`), and nvcc's own implicit/built-in header
search can pull from it independently of `flashinfer`'s explicit `-I`
flags — an internal inconsistency inside `flashinfer==0.6.13`'s own
packaging (or its interaction with a CUDA-13-toolkit-bundled CCCL), not
something this project's own pins control.

**Resolution: `VLLM_USE_FLASHINFER_SAMPLER=0`.** Rather than continue
root-causing a packaging inconsistency inside a third-party dependency's
bundled C++ headers — well outside this issue's scope, and outside what
this project's own pins can fix — `vllm`'s own `envs.py` already defines
`VLLM_USE_FLASHINFER_SAMPLER` (default `True`) specifically to fall back
to a native, pure-PyTorch top-k/top-p sampler
(`vllm/v1/sample/ops/topk_topp_sampler.py`) when the accelerated
`flashinfer` path isn't usable. This is a real, supported vLLM code path
(not a workaround invented here), and issue #6's acceptance criteria is
about vLLM serving working and returning correct completions — not about
`flashinfer`'s specific fused-kernel sampling throughput. Setting
`VLLM_USE_FLASHINFER_SAMPLER=0` let the server start and serve cleanly.

**Final verified result**, run via a fresh `uv sync --extra node` against
the corrected, committed lock — no manual `PATH`/`CUDA_HOME` override
(unlike Attempt 2) — with `CUDA_VISIBLE_DEVICES=0`,
`HF_HOME=/mnt/disk1/Framework/mycelium-hf-cache`,
`VLLM_USE_FLASHINFER_SAMPLER=0`:

- `vllm serve Qwen/Qwen2.5-7B-Instruct --port 8811` → `Application startup complete`, all OpenAI-compatible routes registered including `/v1/chat/completions`.
- Prompt 1: *"What is the capital of France? Answer in one word."* → **`"Paris"`**.
- Prompt 2: *"What is 12 times 7? Answer with only the number."* → **`"84"`**.
- `nvidia-smi` during serving: GPU 0 (the pinned device) at 45640/49140 MiB used; GPUs 1–3 at 4 MiB (idle) — confirms single-GPU pinning worked exactly as designed, not an accidental multi-GPU spread (vLLM's default `gpu_memory_utilization=0.9` preallocates most of the card for KV cache by design — this is not the model's own footprint, which is the ~15 GB bf16 weight size noted above).

**`VLLM_USE_FLASHINFER_SAMPLER=0` is a known limitation to carry
forward**, not silently absorbed: issue #7 (node agent wraps vLLM) will
need to set this same environment variable when it starts the vLLM
process, until/unless the underlying `flashinfer`/CCCL packaging issue is
independently resolved upstream or by a future dependency-pin change.

## Explicitly out of scope for this issue

Fixing `h100`'s GPU permission gap (operator's responsibility,
out-of-band). Testing `paramrudra`'s GPU partition (same SLURM-allocation
tradeoff already declined in issue #5's design). Any node-agent code that
starts/stops/wraps the vLLM process — that's
[issue #7](https://github.com/Zenkai-Dynamics/Mycelium/issues/7). Wiring
`ray.serve.llm` — also issue #7. Tensor parallelism or multi-GPU serving.
Testing with a gated model requiring an HF token/approval.
