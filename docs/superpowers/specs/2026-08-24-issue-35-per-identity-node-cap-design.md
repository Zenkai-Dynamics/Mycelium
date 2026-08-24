# Issue #35 — Per-Identity Node Cap (Sybil Resistance) — Design

Date: 2026-08-24
Status: Implemented
Issue: [#35 — Per-identity node cap (Sybil resistance)](https://github.com/Zenkai-Dynamics/Mycelium/issues/35)
Parent: [#31 — Phase 1: Open node pool to public volunteers](https://github.com/Zenkai-Dynamics/Mycelium/issues/31) —
see [the Phase 1 design doc](2026-08-23-phase-1-open-network-design.md) for
the full phase-level rationale this ticket implements one slice of.

This is the condensed record of the decisions made while brainstorming/
grilling issue #35, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
the Phase 0 issue design docs and [issue #39's design
doc](2026-08-23-issue-39-github-oauth-identity-verification-design.md).

## What issue #35 asks for

The coordinator tracks how many currently-registered node public keys map
to each bound GitHub identity (from #39), and rejects a new registration
once a configurable cap is reached, with a rejection reason distinct from
"invalid token" (extending Phase 0's existing `registration_rejected`
message type). Default cap: 3 — a config value, not hardcoded. A node
disconnecting/unregistering frees up a cap slot for that identity.

## Decisions made

**Cap counting: a live scan over `_nodes` at check time, no new state.**
`NodeRegistry` already has everything needed — `_nodes` (currently
registered public keys) and `_identity_by_key` (public key → bound
`GithubIdentity`, from #39). Counting how many of an identity's public
keys are *currently registered* is: for each node in `_nodes`, look up its
identity via `_identity_by_key`, count matches on `identity.id`. A second
data structure (e.g. an `identity_id -> set of public_keys` reverse index,
updated on every `register()`/`unregister()`) was considered and rejected:
it would give an O(1) cap check instead of O(n) over the node count, but
this repo runs at a scale (dozens to low hundreds of nodes) where the scan
is already instant, and a reverse index adds a second piece of state that
must be kept in sync with `_nodes` — more invariants to maintain and test
for no observable benefit. The scan-based approach also gets AC4 ("a node
disconnecting/unregistering frees up a cap slot") for free: `unregister()`
needs zero changes, since removing an entry from `_nodes` is automatically
reflected in the next scan.

**A new `NodeRegistry.enforce_identity_cap(public_key, identity)` method,
separate from `resolve_identity`.** Called in `_handle_registration` right
after `resolve_identity` succeeds, before `register()`. A `public_key`
already present in `_nodes` is exempt from the cap entirely — it's a
reconnect/replace of an existing slot (handled by `register()`'s own
supersede semantics), not a new one, so it must never be blocked by a cap
check. Otherwise, the method counts identity's currently-registered public
keys (via the scan above) and raises a new `IdentityCapReached` exception
(mirroring `MissingGithubToken`'s plain, registry-owned exception style)
if the count is already at or above the configured cap. Folding this logic
directly into `resolve_identity` was considered and rejected: identity
resolution and cap enforcement are different concerns with different
callers-would-want-to-vary-them-independently properties (a future ticket
might want to check the cap without resolving identity, or vice versa),
and `resolve_identity`'s own docstring already promises it never touches
registration state — mixing in cap logic would break that contract.

**Accepted consequence: a cap-rejected key still gets its GitHub identity
bound.** Because `enforce_identity_cap` needs `resolve_identity`'s result
(which identity does this key belong to?), resolution must happen first —
which means a brand-new public key from an already-at-cap identity still
costs a real GitHub API call and gets permanently recorded in
`_identity_by_key`, even though the registration that triggered it is then
rejected. If that same key retries later (the operator raises the cap, or
another of the identity's nodes disconnects and frees a slot), it won't
need to resupply a GitHub token — the binding already happened. This was
weighed against re-ordering the two checks (impossible — the cap check
needs to know an identity to check the cap *against*, so it structurally
can't run first for a never-before-seen key) and against merging the two
methods into one to avoid the "wasted" bind (rejected above, for the
separation-of-concerns reason). It's also not a new attack surface: the
cap always counts live `_nodes` occupancy, never `_identity_by_key`'s
cumulative binding history, so pre-binding spare keys to an identity buys
an attacker nothing — none of those keys can occupy a slot past the cap
regardless of how many are bound.

**Configuration: `NodeRegistry(token, identity_verifier=None,
per_identity_cap=3)`, threaded through `server.serve(...,
per_identity_cap=None)`, surfaced as a new `mycelium-coordinator
--per-identity-cap` flag (default 3).** Mirrors `identity_verifier`'s own
injection pattern from #39 exactly — `serve()`'s parameter defaults to
`None` and is passed straight through, `NodeRegistry.__init__` is where
the actual default (3) lives, so there's exactly one place that ever needs
to change if the default is retuned. No explicit validation of the flag's
value (e.g. rejecting `0` or negative): argparse's `type=int` already
rejects non-numeric input, and a cap of `0` is a valid (if unusual)
operator choice — it means no identity can register a *new* node while
still leaving already-registered nodes running, which is a legitimate way
to temporarily freeze growth of the pool. Not an "impossible scenario"
worth guarding against.

**Rejection reason: `"identity has reached the maximum of {cap}
registered nodes"`**, reusing the existing `registration_rejected` message
type (matching #33/#39's own precedent) — a new message type wasn't
considered, since every registration-time rejection in this codebase
already goes through this one type with a distinguishing `reason` string.
Distinct from the three github-token-related reasons #39 introduced, so an
operator/volunteer reading the reason can immediately tell "your token was
fine, you're just at your node limit" apart from any GitHub-side problem.

## Explicitly out of scope for this issue

Any change to `mycelium-coordinator-status`'s output beyond what already
exists (#39 already shows each node's bound identity; this ticket adds no
new display of remaining cap headroom or count). Manual ban (#37) and
reputation-weighted selection (#36) — unrelated concerns, no interaction
with this ticket's logic. Any reactive enforcement when the operator
*lowers* the cap below an identity's current live count (e.g. forcibly
disconnecting the excess) — existing nodes are never retroactively
evicted; a lowered cap only affects future registration attempts, matching
how no other config value in this codebase reactively enforces itself
against already-connected state. Rate-limiting or throttling registration
*attempts* themselves (as opposed to capping successful registrations) —
not asked for by this issue, and orthogonal to what a per-identity node
cap is for.
