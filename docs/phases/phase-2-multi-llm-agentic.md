# Phase 2 — Multi-LLM Agentic Flow

Status: Designed, not yet built
Depends on: [Phase 1](phase-1-open-network.md) network of nodes
Related: [Phase 2 design rationale](../superpowers/specs/2026-09-04-phase-2-multi-llm-agentic-design.md),
[ADR-0003 — client-side orchestration](../adr/0003-client-side-orchestration.md),
[CONTEXT.md](../../CONTEXT.md) (glossary)

## Goal

Host multiple *different* LLMs across different nodes, and build an agent
whose flow calls across several of these models rather than just one. This
is the first phase where a single logical task touches more than one host.

## What's decided

- **Orchestration is client-side; the coordinator stays stateless.** A flow
  is N ordinary `complete` round-trips, each naming a model, with the client
  holding context between them. This is forced more than chosen:
  [ADR-0002](../adr/0002-node-transport-model.md)'s live reachability tests
  proved nodes have outbound access only, so a node cannot open a connection
  to another node — host-to-host handoff, one of the two options this doc
  originally posed, is architecturally unavailable. See
  [ADR-0003](../adr/0003-client-side-orchestration.md).
- **Different nodes hosting different models already works** — each node
  registers its own `--model` and routing matches the model string exactly.
  Verified against the code; no work needed.
- **Context crosses as a `messages` array**, client → coordinator → node →
  vLLM, preserving system/user/assistant roles. The existing `prompt` string
  remains a one-message shorthand; sending both is rejected as ambiguous.
- **A minimal client-facing `list_models`** lists currently-served models and
  healthy-node counts, without exposing node fingerprints, bound GitHub
  identities or reputation counters.
- **A model missing mid-flow fails that hop and surfaces to the agent**, as
  today. `list_models` is advisory; the coordinator never silently
  substitutes a different model.
- **Staying within a model's context window is the client's job.** Overflow
  returns a clear typed error — never silent truncation on the node.
- **Client-caused faults stop damaging node reputation.** A defect found
  while grilling: an over-length request currently records a *crash* against
  an innocent node. Phase 2 separates client-caused (4xx) from node-caused
  (5xx/crash/timeout) failures.
- **Mycelium ships primitives plus one reference agent, not an agent
  framework.** The agent lives in `examples/`, deliberately outside the
  installed package.
- **Multi-model-per-node is deferred** — a GPU-memory optimization, not a
  capability unlock; run more nodes instead.
- **Privacy: disclosure, sharpened — not a new mechanism.** Several nodes now
  each see a growing slice of one conversation, including other models'
  outputs. Because orchestration is client-side, the agent author controls
  exactly what each hop carries; that is the honest mitigation, and the docs
  say so plainly.
- **Latency is measured, not assumed.** Client-side orchestration costs
  (N−1) extra client↔coordinator round-trips. Expected to be noise against
  multi-second inference; a live-hardware measurement confirms rather than
  assumes, answering this doc's original open question with data.

## Non-goals

Streaming responses. Conversation persistence. Capability-based routing
("any coding model") — model strings stay exact. Multi-model-per-node.
Opening client-side access to the public (inherited from Phase 1). Anything
Phase 3 (layer/pipeline splitting a single model across farms).

Inherited global non-goals: payments/incentive mechanisms, model
training/fine-tuning, multi-tenant SLAs — see Readme §3.
