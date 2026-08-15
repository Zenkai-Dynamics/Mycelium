# Issue #11 — Coordinator Re-routes When the Active Node Goes Down — Design

Date: 2026-08-16
Status: Approved, not yet implemented
Issue: [#11 — Coordinator re-routes when the active node goes down](https://github.com/Zenkai-Dynamics/Mycelium/issues/11)

This is the condensed record of the decisions made while brainstorming/
grilling issue #11, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
the [issue #1](2026-08-12-issue-1-project-skeleton-design.md) through
[issue #10](2026-08-15-issue-10-coordinator-forwards-client-request-design.md)
design docs.

## What issue #11 asks for

With more than one healthy node hosting the same model, the coordinator
should correctly pick among them and, if the node used for the previous
request goes down, route the next request to a different currently-healthy
node instead. Acceptance criteria: with 2+ healthy nodes hosting the same
model, requests can be served by either; killing the node that handled the
last request causes the next request to succeed via a different healthy
node, with no client-visible failure. This closes Phase 0 success
criterion #3.

Everything this issue needs is already in place except node selection and
failure recovery: #10 built the request-routing path itself
(`router.route_request`, the `"complete"` branch in `server.py`), but
`NodeRegistry.find_node_for_model` always returns the first dict-order
match for a model and #10's `_handle_complete_request` has no retry —
if the picked node turns out to be dead, the client just sees a
`complete_error`.

## Decisions made

**Selection: round-robin across nodes hosting the model, not first-match.**
`NodeRegistry` tracks, per model, the `node_id` it last returned; the next
call returns the next node after it among current candidates, wrapping
around. This is the simplest thing that actually distributes load across
healthy nodes — #10 deliberately punted on this ("first-match... doesn't
pretend to be a fairness algorithm nothing asked for") because nothing
asked for it then; #11 explicitly does. If the last-returned node has since
left the registry, rotation restarts from the front of the current
candidate list — no fairness guarantee is promised across registry churn,
only "don't always pick the same node when several are healthy."

**Failover: only on disconnect, not on timeout.** If the node a request was
routed to turns out to be disconnected (`router.NodeDisconnectedError` —
raised whether the connection was already dead at send time or died while
the coordinator was waiting for a reply), the coordinator tries a different
healthy node for the same model before giving up. A timeout
(`NodeTimeoutError` — the node accepted the request but never replied) is
*not* retried automatically: the node might still be working, and silently
re-running the same prompt on a second node risks double-executing it for
no correctness benefit in Phase 0. A timeout still surfaces as a clear
`complete_error`, matching the phase-0 doc's fail-fast philosophy. A node
explicitly reporting failure (`NodeError`) is likewise surfaced immediately,
not retried — the node worked and told us it failed; trying another node
wouldn't change that outcome for a deterministic completion request.

**Self-healing: unregister a disconnected node immediately, don't wait for
heartbeat.** The moment routing to a node raises `NodeDisconnectedError`,
the coordinator removes it from the registry right there
(`registry.unregister(node.node_id, node.websocket)`), rather than leaving
it registered until #9's ping/pong timeout notices (~50s worst case).
Without this, every request in that window would re-discover the same dead
node and pay the failover cost again. `registry.unregister` is already
identity-checked against the specific websocket, so calling it here can't
race destructively with `_handle_registration`'s own `finally`-block
cleanup for the same connection — whichever runs first wins, the second is
a no-op.

**Retry bound: try every distinct healthy node once, not a fixed attempt
count.** Each node that fails with `NodeDisconnectedError` is added to a
per-request `exclude` set and never retried again within that request. The
loop ends either in success or when the registry has no more candidates
outside `exclude` (`NoHealthyNodeError`) — bounded by the number of
distinct nodes ever registered for the model, so it can't loop forever, and
a third or fourth healthy node (if one exists) still gets a chance rather
than the coordinator giving up after an arbitrary cap.

**No new wire protocol.** This issue changes internal selection/retry logic
only — the client↔coordinator and coordinator↔node message shapes defined
in #10's design doc are unchanged.

## Module layout

- `mycelium/coordinator/registry.py` — `find_node_for_model(model,
  exclude=frozenset()) -> Node | None` replaces the current signature;
  round-robin state added as `_last_returned: dict[str, str]` (model →
  node_id) on `NodeRegistry`. The existing
  `test_find_node_for_model_returns_first_match_when_multiple_host_same_model`
  test is replaced with round-robin-specific tests.
- `mycelium/coordinator/server.py` — `_handle_complete_request`'s single
  `find_node_for_model` + `route_request` call becomes a bounded loop: pick
  a node excluding ones already tried this request, route to it, catch
  `NodeDisconnectedError` specifically to unregister-and-retry, catch the
  broader `RoutingError` (timeout, node error, or exhausted registry) to
  fail immediately. No change to `router.py` itself.

## Testing

Unit tests: round-robin rotation across 2+ nodes for the same model in
`registry.py` (including the case where the last-returned node has since
been removed); in `server.py`, failover-on-disconnect (a dead node is
skipped and the request still succeeds via a healthy one, using the same
fake-websocket-raises-`ConnectionClosed` pattern already established in
`test_router.py`), no-failover-on-timeout (a slow node's timeout surfaces
directly, no second node is ever contacted), and registry-exhausted (every
candidate dead → clear `complete_error`, not a hang). A dedicated
*forced-failover* test — not just the literal acceptance-criterion
scenario — kills a node before it's ever been picked, so round-robin's
first candidate is already the dead one, proving the
disconnect/retry/self-heal path itself fires rather than being incidentally
untested because rotation happened to avoid the dead node anyway.

**Live-hardware verification is part of this issue**, matching #4/#6/#7/
#8/#10. Two node agents run on `a6000`, pinned to different GPUs via the
already-existing `--node-id`/`--gpu` flags (`node/cli.py`), both hosting
`Qwen/Qwen2.5-7B-Instruct`, registered against one real coordinator —
substituting for a second physical machine, since `h100`'s GPU permission
gap for user `bapi` was still unresolved as of #6's design doc and no later
issue's doc records it being fixed. `a6000` has 4× RTX A6000 (48.5GB each),
comfortably enough headroom to run two independent vLLM engines
simultaneously on separate cards. Killing a node uses `kill -9` (SIGKILL),
not a graceful stop — a clean `SIGTERM` would trigger #8's graceful
deregistration handshake and remove the node from the registry before the
next request is even attempted, which would prove nothing about the
disconnect/failover path specifically.

Two scenarios get run for real:
1. **Literal acceptance criterion.** Request 1 succeeds; `kill -9` the node
   that served it; request 2 succeeds via the other node, with no
   client-visible failure.
2. **Forced failover.** `kill -9` a node before any request is sent, so the
   coordinator's first pick is already dead; the very first request still
   succeeds via the other node — proving the disconnect-catch/retry/
   self-heal logic fires for real, not just in simulated tests (round-robin
   alone would satisfy scenario 1 without ever exercising this code path,
   since request 2 naturally rotates to the untouched node regardless of
   whether failover works).

`docs/phases/phase-0-foundation.md`'s open-risks list gets updated to
record success criterion #3 as resolved, the same way #8/#9/#10 recorded
their own resolutions.

## Explicitly out of scope for this issue

Fairness guarantees across registry churn (round-robin is "don't always
pick the same node," not a load-balancing algorithm with formal
properties). Retrying a timed-out or node-reported-failed request on a
different node (see rationale above — deferred as a real Phase 1+
question, not silently forgotten). Any change to node registration,
heartbeat/liveness detection, the shared-token auth model, or the wire
protocol (#8/#9/#10, unchanged). Queueing. Any concurrency cap or admission
control (#10, unchanged). True multi-machine live verification pending
`h100`'s permission fix — noted as a real gap, not the blocking concern of
this issue.
