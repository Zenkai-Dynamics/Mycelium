# Issue #71 — The Coordinator Never Relays a Node's Fault to the Client — Design

Date: 2026-09-18
Status: Approved, not yet implemented
Related: [issue #58's design](2026-09-05-issue-58-fault-classification-design.md)
(introduced `fault`), [issue #60's design](2026-09-05-issue-60-reference-agent-design.md)
(hit this gap and worked around it), [issue #71](https://github.com/Zenkai-Dynamics/Mycelium/issues/71),
[PRD: issue #54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54)

## The defect

`node/request_handler.py` already sets `fault: "node"` correctly on every
failure that is not the client's (`_handle_complete`, broad `except
Exception` branch). But `coordinator/server.py`'s `_handle_complete_request`
throws it away: the `except router.NodeTimeoutError` and `except
router.NodeError` branches both call `reject(str(exc))` with no `fault`
argument, even though `reject()` already accepts one — it is used two
branches up, for `ClientRequestError`. So a client cannot tell "a volunteer's
machine crashed or timed out" from "nothing came back at all"; both arrive as
a `complete_error` with no `fault` field. `docs/OPERATIONS.md` already
documents the `fault == "node"` branch as if it were reachable; it is dead
code, and nothing in the test suite exercises it. Issue #60's reference agent
hit exactly this gap and had to word its own caveats around an ambiguity that
should not exist.

## Decision

**Pass `fault="node"` as a literal at both catch sites.** `NodeError` and
`NodeTimeoutError` already mean "the node's own fault" by construction —
that is precisely why `ClientRequestError` exists as a sibling type rather
than a subclass (#58's design). There is no per-instance value to derive or
thread through the exception hierarchy; it is a fixed constant at each of
the two sites, so the fix is the literal rather than a new `fault` attribute
on the exception classes. Adding one would be structure for a value that
never varies — the thing CLAUDE.md's Simplicity First guidance flags as
overcomplication.

**`NoHealthyNodeError` (the generic `except router.RoutingError` branch)
stays faultless.** This is not a new call — it is #58's design doc's
decision restated: *"no healthy node belongs to neither party."* #71's
acceptance criteria do not ask to revisit it, and this repo's own history
(`test_no_healthy_node_carries_no_fault`) already pins the current behavior
as intentional. Confirmed with the operator before writing this doc rather
than assumed.

Nothing in `router.py` changes. `route_request` already collapses "the node
reported a failure and it wasn't a validated client claim" into `NodeError`
— that collapse is what makes `fault="node"` correct as a fixed literal at
the `except NodeError` site.

## What becomes exact

Today, `docs/OPERATIONS.md`'s `HopError` example and `examples/draft_then_critique.py`
both have to hedge: a `complete_error` with a populated `exposed` and no
`fault` is currently ambiguous between "a node crashed or timed out" and "no
healthy node was available." After this fix, the first case carries
`fault: "node"` and the ambiguity narrows to just the second — which stays
deliberately faultless, so it is not fully resolved, only narrowed to the one
case this project already decided should stay that way.

This also makes `examples/draft_then_critique.py`'s `elif exc.fault ==
"node":` branch reachable for the first time — it exists in the shipped
example today but nothing before this fix can trigger it, and nothing in
`tests/examples/test_draft_then_critique.py` exercises it.

## Scope

- `src/mycelium/coordinator/server.py`: two `reject()` call sites gain
  `fault="node"`; the handler's docstring (which currently says the reply's
  `fault` field is `"client"` or absent) is updated to mention `"node"`.
- `tests/coordinator/test_server.py`: `test_node_reported_failure_still_reports_that_node_as_exposed`
  currently asserts the reply has *no* `fault` key — that assertion flips to
  asserting `fault == "node"`. The timeout-counter test gains the same
  assertion. `test_no_healthy_node_carries_no_fault` is unchanged.
- `examples/draft_then_critique.py`: the comments in `main()`'s exception
  handler and in `_print_exposure()` currently describe this exact gap as
  open and cite #71 by number — they are updated to describe the fix rather
  than the gap.
- `tests/examples/test_draft_then_critique.py`: comments citing #71 as
  pending are updated; a new test is added that exercises the previously
  dead `elif exc.fault == "node":` print branch.
- `docs/OPERATIONS.md`: spot-check only. Its `fault == "node"` example
  already reads as if live; no wording currently claims the branch is
  unreachable, so no change is expected here beyond confirming that during
  implementation.

## Out of scope

Any change to what counts as exposure, to reputation counters, or to the
`reason` string's no-node-naming rule (#63) — none of those change here.
Giving `NoHealthyNodeError` its own `fault` value (see Decision above).
Editing dated spec docs that reference #71 as unresolved (`docs/superpowers/specs/2026-09-05-issue-60-reference-agent-design.md`)
— those are left as-written per this repo's own documentation map.
