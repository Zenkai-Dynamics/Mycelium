# Issue #36 — Reputation-Weighted Node Selection — Design

Date: 2026-08-24
Status: Approved, not yet implemented
Issue: [#36 — Reputation-weighted node selection](https://github.com/Zenkai-Dynamics/Mycelium/issues/36)
Parent: [#31 — Phase 1: Open node pool to public volunteers](https://github.com/Zenkai-Dynamics/Mycelium/issues/31) —
see [the Phase 1 design doc](2026-08-23-phase-1-open-network-design.md) for
the full phase-level rationale this ticket implements one slice of.

This is the condensed record of the decisions made while brainstorming/
grilling issue #36, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
the Phase 0 issue design docs and [issue #35's design
doc](2026-08-24-issue-35-per-identity-node-cap-design.md).

## What issue #36 asks for

Per-node counters for completions / timeouts / crashes / disconnects,
incremented at the exact points `NodeTimeoutError` / `NodeError` /
`NodeDisconnectedError` are already raised today (#10/#11) — no new
instrumentation needed. `NodeRegistry.find_node_for_model` gets a soft
weighting pass using these counters: still round-robin among currently-
healthy nodes, not a hard reliability cutoff — a node with a poor track
record can still be picked, just less preferentially. Counters are
visible via `mycelium-coordinator-status`.

Unlike every other Phase 1 ticket so far, this one is explicitly *not*
gated on #33/#39's identity work — it applies equally to a Phase-0-style
closed pool of operator-trusted nodes.

## Decisions made

**Counters live in a new `public_key`-keyed dict on `NodeRegistry`, not on
the `Node` dataclass.** Tracing the disconnect path makes this the only
correct choice, not a style preference: `route_request` raises
`NodeDisconnectedError`, and `_handle_complete_request` immediately calls
`registry.unregister(...)` on catching it — which deletes that
connection's `Node` object from `_nodes` entirely. Counters stored *on*
`Node` would be discarded in the same breath they were incremented, and
every reconnect would silently zero a node's whole history — exactly
backwards for a feature whose entire point is tracking disconnect-
proneness. A separate dict (mirroring `_identity_by_key`'s existing
pattern) survives independently of whichever `Node` object currently
represents the live connection, keyed by the same `public_key` identity
that already survives reconnects.

**Selection: round-robin when all candidates are equally reliable
(unchanged), a weighted-random draw only when reputations actually
differ.** The issue's own language — "prefers... more often," "can still
be picked" — describes a probability distribution, not a deterministic
rule, so a straight rewrite to always-weighted-random was the literal
reading. Rejected anyway: `find_node_for_model` is 100% deterministic
today, and 8+ existing tests assert exact round-robin sequences against
nodes with no reputation history (the only case those tests construct).
Always-weighted-random would need every one of those tests rewritten as
statistical/seeded instead of exact-sequence — a blast radius this ticket
doesn't need. Nginx-style smooth weighted round-robin (deterministic,
degenerates to plain round-robin at equal weights, no randomness ever)
was considered as the "no flaky tests" alternative — rejected as
materially riskier for this ticket's scope: it replaces
`find_node_for_model`'s entire rotation-state model (`_last_returned` →
per-node running weights), and re-deriving the exact restart-on-node-gone
and exclude-doesn't-mutate-state semantics 8+ existing tests pin down
under a different state model is a much larger, easier-to-get-subtly-
wrong change than this ticket's actual scope calls for. The chosen
approach is a strict superset of today's behavior: compute a weight per
candidate; if every candidate's weight is identical (true whenever none
of them have any recorded history yet, or their histories happen to
match exactly), fall through to the existing rotation logic completely
unchanged; only when weights genuinely differ does a `random.choices(...,
weights=...)` draw happen at all.

**Weight formula: Laplace-smoothed success rate,
`(completions + 1) / (total_attempts + 1)`.** A node with no recorded
history yet scores `1/1 = 1.0` — identical to a node with a flawless
track record, so a brand-new node is never penalized just for being
unproven ("soft weighting," not "distrust the unfamiliar"). The score
decreases smoothly as failures accumulate and is never exactly 0 (a node
with e.g. 10 straight failures scores `1/11 ≈ 0.09` — low, but nonzero,
directly satisfying "never fully excluded... can still be picked"). An
unbounded-growth formula like `1 + completions - failures` was considered
and rejected: it would let a long-running node's sheer accumulated
completion count dominate selection over an equally-reliable but newer
node, a bias the issue never asks for — this is about avoiding
*unreliable* nodes, not preferring *tenured* ones. Disconnects are
counted as failures on equal footing with timeouts/crashes (all three
feed the same `total_attempts` denominator) — the issue's own phrasing
treats all three as the same kind of signal, and all three are only ever
raised in the context of an attempted routed request (see router.py), so
there's no case where a disconnect fires independent of a routing
attempt that would justify scoring it differently.

**RNG: an injected `random.Random` instance, not global `random.seed()`
in tests.** `NodeRegistry.__init__` gains `random_source: random.Random |
None = None`, defaulting to a fresh, private `random.Random()` instance —
deliberately not the `random` module's shared global functions, so
nothing else in the process (another test, another library) can ever
perturb this registry's draws by calling `random.seed()` elsewhere. Tests
inject `random.Random(<fixed seed>)` for fully reproducible weighted-
draw outcomes. Matches this codebase's established injectable-dependency
convention (`identity_verifier`, `per_identity_cap`) rather than
introducing a new, one-off testing idiom.

**`server.py` wiring: three explicit `except` clauses replace today's one
generic `except router.RoutingError`, sharing response logic via a small
local helper.** `_handle_complete_request`'s failover loop currently has
`except router.NodeDisconnectedError` (retry) and one catch-all `except
router.RoutingError as exc` (covers `NodeTimeoutError`, `NodeError`, and
`NoHealthyNodeError` together, all producing the identical send-
`complete_error`-and-close response). Splitting into three named
clauses — `NodeDisconnectedError` (existing, gains a `record_disconnect`
call before `unregister`), `NodeTimeoutError` (new, `record_timeout`),
`NodeError` (new, `record_crash`) — keeps the exception taxonomy
explicit rather than doing an `isinstance` dispatch inside one broad
`except` block, matching the explicit-multi-clause style #39 already
established for its own three-way rejection split. `NoHealthyNodeError`
keeps falling through the remaining generic `except router.RoutingError`
clause unchanged — there's no specific node to attribute a counter to
when none was ever picked. To avoid tripling the sent-response
boilerplate across three now-separate except blocks, a small local
`async def _reject(reason): ...` closure (defined once at the top of
`_handle_complete_request`) holds the shared send/close logic; each
except block records its counter, calls `await _reject(str(exc))`,
returns. The success path (today's `else: break`, reached once
`route_request` returns without raising) calls `record_completion`
before falling through to the existing result-send logic.

**Display: `mycelium-coordinator-status` gains a compact bracketed
segment.** `list_nodes()` adds a `"reputation"` field per node:
`{"completions": N, "timeouts": N, "crashes": N, "disconnects": N}`.
`status_cli.py`'s print loop renders it as
`[ok:N timeout:N crash:N disconnect:N]`, e.g.
`nodeA [a1b2c3d4e5f6] (github:octocat) [ok:12 timeout:1 crash:0 disconnect:2]: Qwen/Qwen2.5-7B-Instruct`
— consistent with the existing bracketed-fingerprint and parenthetical-
identity conventions #33/#39 already established in that same output
line.

## Explicitly out of scope for this issue

Any time-based decay or aging of counters (e.g. "only count the last N
attempts" or exponential weighting toward recent history) — the issue
doesn't ask for it, and it adds real complexity (a rolling window or
timestamped log per node) for a benefit nobody requested; a node's
counters simply accumulate for the coordinator process's lifetime,
exactly matching how #35's per-identity cap and #39's identity binding
already treat all their own in-memory state. A hard reliability cutoff
(refusing to route to a node below some threshold) — the issue explicitly
rules this out ("not a hard reliability cutoff"). Any interaction with
#35's per-identity cap or #37's manual ban — unrelated concerns, no
shared state or logic. Persisting counters across a coordinator restart —
matches every other piece of in-memory-only coordinator state in this
codebase.
