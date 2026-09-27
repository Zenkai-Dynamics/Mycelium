# Issue #56 — Client-Facing Model Discovery — Design

Date: 2026-09-06
Status: Approved, not yet implemented
Related: [Phase 2 implementation design](2026-09-04-phase-2-implementation-design.md),
[issue #59's design](2026-09-05-issue-59-flow-library-design.md) (the library this extends),
[issue #56](https://github.com/Zenkai-Dynamics/Mycelium/issues/56),
[PRD: issue #54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54)

A client can ask the coordinator which models are currently served, so an
agent can be written against what actually exists rather than guessing model
strings and failing at call time.

## Decisions

### The response is deliberately impoverished

Distinct model strings and a healthy-node count for each. No key
fingerprints, no bound GitHub identities, no reputation counters — those are
operator-facing, and a client token must not be able to enumerate volunteer
identities.

This leaks nothing material: a client already holds the shared token and can
probe any model string directly by attempting a completion, so `list_models`
reveals strictly less than trying, and far less than the operator status view.

**It is therefore a separate registry method, not a filter over
`list_nodes()`.** Deriving the client view by stripping fields from the
operator view would mean every future field added for operators is exposed by
default and must be remembered to strip. Building it up from nothing inverts
that: a new operator field appears on the client wire only if someone
deliberately puts it there.

### Sorted by model string

Registry iteration is insertion order, so an unsorted response would shuffle
as volunteers come and go — unstable output for anyone comparing two runs,
and tests whose result depends on registration sequence. Sorting makes the
response deterministic for a given registry state.

An empty registry returns an empty list rather than an error. "Nothing is
served right now" is a true and useful answer, not a failure.

### "Healthy" means registered

The registry has no other notion of health. A node that stops answering the
WebSocket keepalive is evicted by machinery that already exists, so
registration *is* liveness.

Worth restating from the ticket: this is **advisory only**. A node can
disconnect between the check and the call, so `list_models` can never be a
guarantee. Behaviour when a model turns out to be unavailable at call time is
unchanged — the hop fails and surfaces to the caller, and the coordinator
never silently substitutes a different model.

### `Flow.list_models()` lands here

#59 deliberately omitted it because the wire request did not exist; the
decision at the time was "add it when #56 lands". This is that moment, not
new scope.

It also matters for [#61](https://github.com/Zenkai-Dynamics/Mycelium/issues/61):
that ticket's first acceptance criterion asks that both models be
"discoverable **by a client**", which the live session had to record as only
partially satisfiable precisely because client discovery did not exist. With
this method, a resumed #61 can meet the criterion as worded.

### The new CLI uses the shared transport

`mycelium-client-models`, flat argparse, matching the
`mycelium-coordinator-status` / `mycelium-coordinator-ban` precedent rather
than introducing subcommands. Human-readable lines, matching how the operator
CLIs print — agents wanting programmatic access have `Flow.list_models()`,
which is a better answer than JSON on stdout.

It goes through `client/transport.py`. That module was extracted in #59
exactly so the next client-side caller would stop hand-rolling
connect-send-receive against a wire that #55, #58 and #63 have each changed.
This is the first new caller since, and hand-rolling a fourth copy would
waste the extraction.

*(The operator CLIs `status_cli` and `ban_cli` still hand-roll their own.
Migrating them is a defensible follow-up and explicitly not this issue's
work.)*

### Gating, and the #68 boundary

Gated by the same shared client token as `complete`.

[#68](https://github.com/Zenkai-Dynamics/Mycelium/issues/68) is open and asks
whether operator-facing requests — `status_query`, `ban_identity` — should
require a credential distinct from the client token. Whatever it decides,
**`list_models` stays client-gated**: it is the example of what a client
token *should* be able to do, and its response was designed from the start to
be safe in client hands.

Stated here so #68 does not sweep it up by accident when it separates the
operator surface.

### Discovery gets its own timeout, not the completion one

Both client entry points — `Flow.list_models` and `mycelium-client-models`
— share a `DISCOVERY_TIMEOUT_SECONDS` / `LIST_MODELS_TIMEOUT_SECONDS` pair
(10s), pinned equal by a test the same way the completion pair already is.

It is deliberately not `CALL_TIMEOUT_SECONDS` (140s): that value's own
comment justifies it as "10s past the coordinator's per-attempt
`NODE_COMPLETE_TIMEOUT_SECONDS`, so the coordinator's timeout fires
first" — reasoning about a routed request retried across nodes. A
`list_models` request never reaches a node; the coordinator answers it
straight out of its in-memory registry. There is no per-attempt timeout
to sit behind and no failover loop to survive, so a short, independent
timeout is the honest one: the coordinator either answers quickly or it
is not going to.

## Testing

Follows the existing suite: real local websocket connections, fake external
dependencies, no real GitHub or vLLM.

- Two nodes on different models — both appear, with correct counts; two nodes
  on the *same* model appear once with a count of two.
- A model whose only node disconnects stops appearing.
- **The reply's key set is asserted exactly**, not just checked for absent
  fields — following the discipline #63 established, so a field nobody
  thought to forbid cannot reach a client-facing wire unnoticed. Also assert
  no fingerprint, identity or reputation value appears anywhere in the
  serialised reply.
- A wrong token gets the connection closed with no reply, exactly as other
  client requests do.
- `Flow.list_models()` returns the parsed list, and lets `TransportError`
  propagate on a failure to reach the coordinator.

On that last point: a discovery failure is **not** a hop, so raising
`HopError` would be wrong — there is no `Hop` to attach, no `fault` to
report, and nothing to add to the flow's record. `TransportError` is what
actually happened, so it propagates unwrapped and is added to
`mycelium.client`'s exports, giving agent authors one name to catch rather
than reaching into a submodule.

## Out of scope

Capability-based routing ("any coding model") — model strings stay exact.
Migrating the operator CLIs onto the shared transport. Any change to how
routing picks a node. Anything #68 decides about operator credentials.
