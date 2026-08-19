# Phase 0 — Foundation

Status: **Building now**
Depends on: nothing (first phase)
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md) · [Node heartbeat & liveness tracking](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md) · [Coordinator forwards a client request](../superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md) · [Coordinator re-routes when the active node goes down](../superpowers/specs/2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md) · [Clean, immediate failure when no healthy node is available](../superpowers/specs/2026-08-16-issue-12-clean-immediate-failure-no-healthy-node-design.md)

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

**Failure handling:** if no healthy node is hosting the requested model, the coordinator fails the request immediately with a clear error. No queueing in Phase 0; the only retry is a bounded, disconnect-only failover to a different healthy node when the one first picked turns out to be dead (#11) — a slow-but-alive node or an explicit node-reported failure is never retried.

## In scope

- Node registration + heartbeat/health-check against the coordinator
- Routing a single request to a single healthy node
- One HF model — [`Qwen/Qwen2.5-7B-Instruct`](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct), chosen to fit comfortably on the smallest node's VRAM (~15 GB in bf16, well under a single 48 GB A6000 GPU) and confirmed to serve real completions correctly via `vllm serve` on real target hardware — see [the model-choice design doc](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) for the selection rationale and the real-hardware verification
- A basic client interface to send a prompt and get a completion back

## Explicitly out of scope

- Public/untrusted node onboarding, sandboxing, or output verification
- Multiple simultaneous models, or load-balancing guarantees across nodes beyond #11's round-robin (fairness across registry churn, weighted routing, etc.)
- Any cross-host context or activation passing
- Queueing; retrying a timed-out or node-reported-failed request on a different node (#11 added disconnect-only failover across healthy nodes, but not this)

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
- **Node heartbeat/liveness: resolved.** The coordinator tracks node liveness using the WebSocket ping/pong keepalive already established by #5/#8 (`PING_INTERVAL_SECONDS`/`PING_TIMEOUT_SECONDS`, both 20s) rather than a separate application-level heartbeat message — a node that goes silent, whether cleanly killed or unresponsive due to a network partition or frozen process, is dropped from the registry within ~50s worst case (`PING_INTERVAL_SECONDS + PING_TIMEOUT_SECONDS + CLOSE_TIMEOUT_SECONDS`, the last of which #9 added as an explicit constant after finding it was previously an unnamed library default). The clean-kill case was already live-verified by #8; #9 adds a regression test for the silent-disconnect case specifically. See [the design doc](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md) for the full decision record.
- **Multi-node failover: resolved, live-verified.** With 2+ healthy nodes hosting the same model, the coordinator round-robins across them (`NodeRegistry.find_node_for_model`, per-model rotation state) rather than always picking the same one. If the node a request gets routed to turns out to be disconnected, the coordinator self-heals the registry immediately (doesn't wait for #9's ping/pong timeout) and retries a different healthy node before failing — so killing the node that handled the last request never produces a client-visible failure as long as another healthy node exists. A timeout or a node-reported failure is surfaced immediately rather than retried, to avoid double-running a prompt on two nodes. Live-verified by #25 on real hardware: two real node agents (`a6000`, separate GPUs), a real coordinator on a separate third-party host, and a real client on a fourth machine — both the literal failover scenario and a forced-failover scenario (killing a node *before* any request, proving the disconnect-catch/self-heal path specifically, not just round-robin luck) round-tripped correctly with no client-visible failure. See [the design doc](../superpowers/specs/2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md) for the full decision record and live-verification narrative.
- **Client request routing: resolved.** The coordinator accepts a client's completion request over the same TLS WebSocket port nodes use (`{"type": "complete", "token", "model", "prompt"}`), authenticated with the same shared token, picks the first registered node hosting the requested model, and forwards the request over that node's already-open connection, correlated by a per-request `request_id`. The node runs it through the local `VLLMProcess.complete()` built in #7 and replies; the coordinator relays the result (or a clear error — no healthy node, the node timed out, the node disconnected mid-request, or the node itself reported a failure) back to the client unchanged. See [the design doc](../superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md) for the full decision record.
- **Immediate failure with no healthy node: resolved.** A client request for a model with zero currently-registered nodes fails on the very first, single `NodeRegistry.find_node_for_model` lookup — no wait, no queue, no retry. Built and live-verified as part of #10 (`error: no healthy node for model '...'`, 0.111s, exit 1, against a real coordinator with no nodes registered); #12 adds a dedicated regression test confirming both the immediacy and, structurally (an invocation-count assertion, not just timing), that no retry/poll loop runs. See [the design doc](../superpowers/specs/2026-08-16-issue-12-clean-immediate-failure-no-healthy-node-design.md) for the full decision record.

## Next step

Phase 0's core happy path (client → coordinator → node → vLLM → response) is implemented and live-verified as of issue #10 — see success criterion #2 above. #9 covers a single dead node being dropped from routing; #11 extends that to the multi-node case — round-robin selection plus immediate failover when the currently-picked node is the one that just died — closing success criterion #3. #12 ("Clean, immediate failure when no healthy node is available") is resolved — success criterion #4's behavior was already built and live-verified under #10; #12 added the regression test that locks it in, with no production code changes needed. #25 closed the one remaining gap: live-hardware verification of #11's two-node failover, run against two real node agents on `a6000`, a real coordinator on a separate host, and a real client on a fourth machine — which also stands as criterion #1's two-real-node live verification (registration + heartbeat, both confirmed working on real hardware).

**All four Phase 0 success criteria are now resolved and live-verified — Phase 0 is functionally complete.** Two secondary items remain open but don't block that: outbound egress from `paramrudra`'s actual HPC *compute* node (as opposed to its login node) is still untested, and true verification across two genuinely separate physical GPU hosts (as opposed to two GPUs on one host, or `a6000` alongside a permissions-blocked `h100`) hasn't happened yet. Neither was ever part of the stated success bar above; both are worth closing before treating the node pool as trustworthy at real multi-operator scale, but Phase 0 as scoped is done.
