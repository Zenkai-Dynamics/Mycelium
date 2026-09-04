# 3. Multi-model flows are orchestrated client-side; the coordinator stays stateless

## Status

Accepted — 2026-09-04. Introduced by Phase 2 (multi-LLM agentic flow).

## Context

Phase 2 lets one logical task span several models on several nodes. Something
has to decide the order of hops and carry the accumulated context between
them. The phase doc left this open from the very beginning of the project:

> "the context either remains at user's machine or is passed to the llm host
> — idk how should we move on that — but somehow we have to pass at least
> some parts from one host to another."

One of those two options is not actually available. [ADR-0002](0002-node-transport-model.md)
established from real reachability tests that nodes have outbound access
only — private RFC1918 addresses behind a university VPN, and an HPC login
node that silently drops every port but one. **A node cannot open a
connection to another node.** Context can therefore only cross hosts via the
client or via the coordinator.

## Decision

**The client orchestrates. A flow is N ordinary `complete` round-trips, each
naming a model, with the client holding the context between them. The
coordinator continues to relay single requests and remembers nothing about
conversations.**

## Considered Options

**Coordinator-side orchestration** — the client submits one task and the
coordinator drives the hops. Rejected for now: it makes the coordinator
stateful for the first time, and it would put full conversation state in the
one component every request already passes through. Its real advantage is
fewer wide-area round-trips, which matters only if client↔coordinator latency
is significant next to inference time — an assumption we chose to measure
rather than design around.

**Node-driven, coordinator relaying** — a node orchestrates and asks the
coordinator to forward context onward. Rejected: it makes a volunteer's
machine the flow controller and requires new node→coordinator→node brokering,
the largest change to the trust model of the three.

## Consequences

- The coordinator's statelessness — a property since Phase 0 — is preserved,
  and Phase 2 requires no new coordinator state.
- The client chooses exactly what each node sees on each hop. This is the
  only privacy control available, since a node must read the plaintext it
  serves (see Phase 1's disclosure in `docs/OPERATIONS.md`), and Phase 2
  widens exposure: several nodes now each see a slice of one conversation,
  including other models' outputs.
- A flow costs (N−1) extra client↔coordinator round-trips versus
  coordinator-side orchestration. Expected to be noise against multi-second
  inference; Phase 2 includes a live-hardware measurement to confirm rather
  than assume.
- Reversal is additive, not a rewrite: the wire protocol is unchanged by
  where orchestration happens, so a coordinator-side orchestrator could be
  added later if measurement justifies it.
