# Phase 0 — Foundation

Status: **Building now**
Depends on: nothing (first phase)
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md)

## Goal

Prove the basic loop works end to end: a client sends a prompt, it reaches
one of a handful of GPU machines the operator personally controls, and a
response comes back — before any of the hard distributed-systems problems
(public trust, multiple models, model parallelism) are in scope.

## Scope

One LLM. A small, closed pool of nodes the operator personally controls
(HPC allocations, VPN-gated lab machines).

## Architecture

**Components:**

- **Coordinator** — a single, publicly-reachable service. Holds the registry of currently-online nodes and which model each is hosting; routes an incoming client request to a healthy node; is the one stable address a client ever talks to.
- **Node agent** — runs on each opted-in GPU machine. Registers with the coordinator, sends heartbeats, and manages a local inference engine serving one HF model. Takes inspiration from the gateway/serve split already prototyped in the operator's [Bhaskera](https://github.com/dcll-iiitd/Bhaskera/tree/serve) framework, wrapping vLLM (optionally Ray-orchestrated, matching that prior work) rather than reimplementing serving from scratch.
- **Client** — sends a request to the coordinator and gets a response back, unaware of which node handled it.

**Data flow:** `Client → Coordinator (picks a healthy node hosting the requested model) → Node agent → vLLM → response → back up the chain`

**Coordination model:** centralized, not peer-to-peer. With a handful of trusted nodes, a single coordinator is simplest to build and debug; decentralized discovery is deferred to Phase 1, where "opt-in from strangers" makes a single trust anchor less appropriate.

**Failure handling:** if no healthy node is hosting the requested model, the coordinator fails the request immediately with a clear error. No queueing or retry logic in Phase 0.

## In scope

- Node registration + heartbeat/health-check against the coordinator
- Routing a single request to a single healthy node
- One HF model — [`Qwen/Qwen2.5-7B-Instruct`](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct), chosen to fit comfortably on the smallest node's VRAM (~15 GB in bf16, well under a single 48 GB A6000 GPU) and confirmed to serve real completions correctly via `vllm serve` on real target hardware — see [the model-choice design doc](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) for the selection rationale and the real-hardware verification
- A basic client interface to send a prompt and get a completion back

## Explicitly out of scope

- Public/untrusted node onboarding, sandboxing, or output verification
- Multiple simultaneous models or multi-node load balancing
- Any cross-host context or activation passing
- Queueing, retries, or failover

## Success criteria

Phase 0 is done when:
1. Two or more real nodes (the operator's HPC/VPN-gated machines) register and heartbeat successfully with the coordinator.
2. A client sends one prompt; it round-trips correctly through coordinator → node → vLLM → response.
3. Killing a node removes it from routing — no request gets sent to a dead node.
4. A request made when no node is healthy fails with a clear, immediate error rather than hanging.

## User stories

1. As a node operator, I want to opt a GPU machine into the pool with minimal setup, so that I can contribute idle capacity without babysitting it.
2. As a node operator, I want the coordinator to stop routing to my machine automatically if it goes offline, so that clients never hit a dead node.
3. As a client, I want to send a prompt to one stable address and get a completion back, so that I don't need to track which physical machine is currently serving the model.
4. As the operator, I want a clear failure when no node is available, so that I can tell "system down" apart from "request took forever."
5. As the operator, I want Phase 0's architecture to not paint Phase 1 (public nodes) into a corner, so that opening the pool later doesn't require rearchitecting the coordinator/node split.

## Open risks / unresolved decisions

- **Node network reachability: resolved.** Confirmed against real candidate nodes — neither the VPN-gated lab machines (`a6000`/`h100`, private addresses, unreachable from the public internet by definition) nor the CDAC/IUAC HPC login node (`paramrudra`, a public IP where no port tested other than its SSH port answers from outside) accept inbound connections on an arbitrary port. The node agent dials out to the coordinator and holds the connection open — the BOINC/ngrok/torrent-tracker pattern this doc already anticipated. See [ADR-0002](../adr/0002-node-transport-model.md) for the full investigation and decision.
- **Outbound egress from node environments is unverified.** All tests so far confirmed inbound unreachability, not that nodes can dial *out* — and `paramrudra`'s evidence covers its login node only, while the HPC compute node that would actually run vLLM commonly has more restricted egress. Needs testing before the dial-out transport is built.
- **Node auth mechanism: resolved.** A single shared token, delivered to both `mycelium-node` and `mycelium-coordinator` via `--token-file` (never a bare CLI flag or environment variable), validated with `hmac.compare_digest`. A node sends its token, model, and a self-reported (or hostname-defaulted) node ID on every fresh connection; the coordinator rejects an invalid or missing token with a clear reason before closing the connection, and a registered node is queryable by an operator via a new `mycelium-coordinator-status` command. Verified live against a real coordinator and a real `vllm serve`-backed node on `a6000`: successful registration, a rejected bad token (both via the status command and a raw registration attempt), and a clean deregistration on `SIGTERM`. See [the design doc](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md) for the full decision record and live-verification narrative.
- **Exact model choice: resolved.** `Qwen/Qwen2.5-7B-Instruct` was confirmed to run correctly via `vllm serve` on a real target node (`a6000`, single RTX A6000 GPU, pinned via `CUDA_VISIBLE_DEVICES=0`) — a real prompt returned the correct completion. Getting there also required fixing a real dependency-version drift (`nvidia-cuda-nvcc` vs. `nvidia-cuda-runtime`, now corrected in `pyproject.toml`/`uv.lock`). See [the design doc](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) for the full investigation.
- **vLLM must be started with `VLLM_USE_FLASHINFER_SAMPLER=0`: resolved in code.** A packaging inconsistency in flashinfer's bundled CCCL/cub headers breaks its JIT-compiled sampling kernel (unrelated to the CUDA toolchain fix above) — vLLM's own native-sampler fallback works correctly. The node agent (issue #7) now sets this env var unconditionally every time it starts `vllm serve`, and automatically starts/stops the vLLM subprocess (process-group kill, no orphaned GPU processes — including vLLM's own worker subprocesses, confirmed against real hardware) and forwards prompts to it. See [the node agent design doc](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) for the implementation decisions and live-hardware verification, and [the original model-choice design doc](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) for the underlying investigation.

## Next step

Once this document and the risks above are settled, write a Phase 0 implementation plan (not yet started).
