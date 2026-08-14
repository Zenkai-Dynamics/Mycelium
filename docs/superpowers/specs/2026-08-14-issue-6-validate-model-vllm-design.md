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

**Model storage: `HF_HOME` redirected to `/mnt/disk1/mycelium-hf-cache`.**
`a6000`'s root disk is already at 95% full (51 GB free) and is a machine
shared with several other users/projects — downloading ~15 GB of model
weights there would work today but leaves little margin and tightens a
disk other people also depend on. `/mnt/disk1` has 269 GB free and
already hosts other projects' data on this box; a dedicated
`mycelium-hf-cache` subdirectory there keeps this project's footprint
contained and out of the tight root disk entirely.

**Verification: the OpenAI-compatible `/v1/chat/completions` endpoint,**
not the raw `/v1/completions` endpoint — matches how an Instruct-tuned
model is actually meant to be used, and is the interface a real client
would eventually talk to.

**No new ADR.** Unlike [ADR-0002](../adr/0002-node-transport-model.md)
(issue #4), this is a parameter/spec choice (which model, sized how)
rather than an architectural decision — issue #6's acceptance criteria
only asks for the `docs/phases/phase-0-foundation.md` update, and that's
sufficient here.

## Explicitly out of scope for this issue

Fixing `h100`'s GPU permission gap (operator's responsibility,
out-of-band). Testing `paramrudra`'s GPU partition (same SLURM-allocation
tradeoff already declined in issue #5's design). Any node-agent code that
starts/stops/wraps the vLLM process — that's
[issue #7](https://github.com/Zenkai-Dynamics/Mycelium/issues/7). Wiring
`ray.serve.llm` — also issue #7. Tensor parallelism or multi-GPU serving.
Testing with a gated model requiring an HF token/approval.
