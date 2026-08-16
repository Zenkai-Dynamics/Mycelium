# Issue #12 — Clean, Immediate Failure When No Healthy Node Is Available — Design

Date: 2026-08-16
Status: Approved, not yet implemented
Issue: [#12 — Clean, immediate failure when no healthy node is available](https://github.com/Zenkai-Dynamics/Mycelium/issues/12)

This is the condensed record of the decisions made while brainstorming/
grilling issue #12, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
the [issue #1](2026-08-12-issue-1-project-skeleton-design.md) through
[issue #11](2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md)
design docs.

## What issue #12 asks for

When a client requests a model with zero currently-healthy nodes
registered for it, the coordinator should fail the request immediately
with a clear error, rather than hanging, timing out silently, or queueing.
Acceptance criteria: an immediate, clearly-worded error is returned; no
retry or queueing behavior occurs, confirmed by test. This closes Phase 0
success criterion #4.

## Decisions made

**No production code changes.** Tracing the request path for a model with
zero registered nodes: `NodeRegistry.find_node_for_model` returns `None`
on its first (and only) call → `server._handle_complete_request` raises
`router.NoHealthyNodeError` immediately → caught by the `except
router.RoutingError` branch → a `complete_error` reply is sent and the
connection closes. This is a single synchronous dict lookup with no wait
states, no loop, and no queue involved — built and live-verified as part
of #10 (`error: no healthy node for model 'Qwen/Qwen2.5-7B-Instruct'`,
0.111s, exit 1). It holds regardless of whether #11's retry loop has
merged: `NoHealthyNodeError` is never a `NodeDisconnectedError`, so it
never enters #11's retry branch — a model nobody has ever registered for
fails on the very first lookup, same as before #11. There is no gap to
close in production code; #12 is a regression-test issue, not a feature
issue.

**Confirm "no retry" structurally, not just by timing.** A test asserting
only that the response arrives quickly can still pass even if a future
change added a short retry/poll loop — timing assertions are inherently
fuzzy. Instead, `registry.find_node_for_model` gets wrapped (via
`monkeypatch`) to count invocations for the request; the test asserts it
was called exactly once. This makes the "no retry" guarantee resistant to
a future refactor accidentally reintroducing polling, rather than relying
on wall-clock thresholds alone. A fast-elapsed-time assertion is kept
alongside it (matching the existing `elapsed < 3.0`-style pattern already
used in this file for other fail-fast paths), since "immediate" is itself
part of the acceptance criterion, not just "eventually correct."

**No new live-hardware verification.** #10's own PR already exercised
this exact scenario against real hardware (a real coordinator, no nodes
registered, a real `mycelium-client` request) and got the immediate,
correctly-worded error. Re-running the same scenario for #12 would verify
nothing new; the value #12 adds is the regression test, which is what
protects the behavior going forward.

**Independent of #11.** #12's own "Blocked by" lists only #10 (merged).
#11 (round-robin/failover) is still an open PR at the time #12 is
designed; #12 doesn't depend on it and is based on current `main`. The
zero-nodes-ever-registered case this issue tests is unaffected by whichever
of #11/#12 merges first — the two touch overlapping code
(`_handle_complete_request`) but not the same branch of it, so at most a
trivial rebase is needed for whichever merges second.

## Module layout

- `tests/coordinator/test_server.py` — one new test,
  `test_complete_request_with_no_healthy_node_fails_fast_with_no_retry`,
  added near the existing `test_complete_request_with_no_matching_node_returns_error`.
  No other file changes.

## Testing

The new test: a client requests a model no node has ever registered for,
against a coordinator with an empty registry. Asserts (1) the reply is
`complete_error` with the model name in the reason, (2) elapsed time is
well under a generous bound (matching this file's existing fast-fail
convention), and (3) `find_node_for_model` was invoked exactly once
(via a monkeypatched wrapper counting calls), proving no retry/poll loop
ran.

## Explicitly out of scope for this issue

Any change to `router.py`, `registry.py`, `server.py`, the wire protocol,
node registration, heartbeat/liveness, or #11's round-robin/failover logic
(unchanged, and this issue doesn't depend on it). Live-hardware
verification (already covered by #10's PR, see above).
