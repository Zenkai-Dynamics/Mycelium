# 5. Pipeline stages are chained by the coordinator, which holds per-session routing

## Status

Accepted — 2026-10-18. Introduced by Phase 3 (model parallelism).

## Context

Phase 3 splits one model's layers across several stage nodes, so
activations must cross from one stage to the next on every forward pass —
during decode, once per verified batch of tokens. [ADR-0002](0002-node-transport-model.md)
established that nodes are outbound-only: **a stage node cannot open a
connection to the next stage.** Activations can therefore cross only via
the coordinator or via the client.

Each stage also holds a KV cache for the generation in progress, so every
pass of one generation must reach the *same* nodes. Something must
remember which nodes those are.

[ADR-0003](0003-client-side-orchestration.md) kept the coordinator
stateless by having the client orchestrate Phase 2's flows. Applied
literally to Phase 3, that is Petals' model: the client sends activations
to each stage in turn.

## Decision

**The client sends one pass to the coordinator; the coordinator forwards
the activations through the pinned stages in order over the nodes'
existing connections and returns the final hidden states. The coordinator
holds `session_id → [pinned nodes]` for each generation's lifetime —
routing only, never prompts, tokens or activations beyond the pass in
flight.**

## Considered Options

**Client drives each stage** — keeps the coordinator stateless. Rejected:
every stage costs a full client↔coordinator round trip, roughly doubling
the wide-area legs per pass, against an interactive-speed target.

**Direct node-to-node via NAT traversal or an overlay network** — lowest
latency. Rejected: it reopens ADR-0002; hole punching through institutional
firewalls often fails, so a relay fallback is needed anyway; and it gives
volunteers direct reachability to one another, a change to the trust model.

## Consequences

- The coordinator holds per-session state for the first time. ADR-0003's
  statelessness is scoped rather than reversed: the coordinator still never
  holds conversation content, and Phase 2 flows are unaffected.
- Session state must be bounded: dropped on session end, on a node fault,
  and on an idle timeout, so a vanished client cannot pin routing state or
  volunteer VRAM indefinitely.
- A coordinator restart loses every in-flight session. The client library
  recovers by replay — it holds every token — exactly as it does for a
  stage failure.
- The coordinator carries activation bytes: ~16 KB per token per stage
  boundary for a hidden size of 8192 in bf16, so prefill of a long prompt is
  tens of megabytes. Coordinator bandwidth becomes a real cost.
- One relay leg per stage boundary remains. Speculative decoding (in
  Phase 3's scope) amortizes it across several tokens per pass.
