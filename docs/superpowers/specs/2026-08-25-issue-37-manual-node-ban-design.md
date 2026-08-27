# Issue #37 — Manual Node Ban (Operator Override) — Design

Date: 2026-08-25
Status: Approved, not yet implemented
Issue: [#37 — Manual node ban (operator override)](https://github.com/Zenkai-Dynamics/Mycelium/issues/37)
Parent: [#31 — Phase 1: Open node pool to public volunteers](https://github.com/Zenkai-Dynamics/Mycelium/issues/31) —
see [the Phase 1 design doc](2026-08-23-phase-1-open-network-design.md) for
the full phase-level rationale this ticket implements one slice of.

This is the condensed record of the decisions made while brainstorming/
grilling issue #37, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
the Phase 0 issue design docs and [issue #36's design
doc](2026-08-24-issue-36-reputation-weighted-selection-design.md).

## What issue #37 asks for

A new operator-facing command, sibling to `mycelium-coordinator-status`,
lets the operator revoke a specific node's bound identity (from #39)
outright — the backstop for cases reputation signals (#36) don't catch,
e.g. a report of bad-faith output that never trips a timeout or crash. A
banned identity's future registration attempts are rejected the same way
an invalid token is today. Already-connected nodes under a newly-banned
identity are disconnected, not just blocked from future registration. No
automated appeals/dispute flow — banning is a manual operator action only.

## Decisions made

**Ban target: the operator specifies a GitHub login, not the raw numeric
id.** `mycelium-coordinator-status` already shows `(github:<login>)` for
every node — that's the only identity-related string the operator ever
actually sees, so `--identity <login>` requires no new lookup step or
display field. Internally, `ban_identity` resolves the supplied login to
the stable numeric `id` by scanning currently-known identity bindings
(`_identity_by_key`'s values) for a match, and it's that `id` — not the
login — that actually gets recorded as banned and checked on every future
registration attempt. This matters: `id` is immutable across GitHub
username renames, while `login` isn't, so a banned user renaming their
GitHub account doesn't let them back in. Banning by raw numeric id was
considered and rejected as needlessly precise for a first cut — nothing
in this system exposes that number to an operator today, and adding a
display field just to support banning would be scope creep beyond what
the ticket asks for.

**No pre-emptive bans, no unban command.** The issue's own framing —
"revoke a specific node's *bound* identity" — presupposes the identity
already exists as something #39 created; `ban_identity` raises
`UnknownIdentity` if the coordinator has never seen the given login bind
to any public key. Building a way to pre-emptively ban a GitHub account
that's never tried to join was rejected as unrequested scope: the trust
model this phase operates under is reactive (something goes wrong, the
operator responds), not a blocklist maintained independent of the pool.
An explicit unban command was considered and rejected for the same
reason the issue itself gives for skipping an appeals flow — this is a
manual, deliberately blunt operator instrument for a first cut, not a
policy system with a reversal path. Since ban state is in-memory only
(consistent with every other piece of coordinator state — identity
bindings, the per-identity cap, reputation counters), a coordinator
restart already provides an implicit "reset everything" escape hatch if
a ban turns out to be a mistake, matching how the operator would already
have to accept a cap or reputation reset on restart today.

**Disconnecting live nodes: `NodeRegistry.ban_identity()` identifies and
returns matching nodes; it does not unregister them itself.** Marking an
identity banned and forcibly dropping its currently-connected nodes are
two different concerns, and the second one already has a working
implementation: `_handle_registration` already closes a superseded
connection's websocket via `_close_in_background(...)` and lets that
connection's own long-lived handler notice `ConnectionClosed` and run its
existing `finally` block (registry.unregister + failing any pending
routed requests). `ban_identity` reuses this exact path rather than
duplicating it: it marks the identity's `id` banned, then returns the
list of currently-registered `Node` objects under that identity so
`server.py` can call the same `_close_in_background` on each of their
websockets. Having `ban_identity` also call `unregister` directly on each
match was considered — rejected: it would mean two different code paths
independently reimplementing "a node's connection ended, clean up after
it" (one for natural disconnects, one for bans), with the ban path
missing the pending-request-failure half unless it duplicated that logic
too. The only cost of the chosen approach is a benign, brief window
between the ban command being processed and each closed socket's own
cleanup actually running — the same kind of overlap the superseded-
connection path already accepts today.

**`enforce_not_banned(identity)` — new method, same shape as
`enforce_identity_cap`, checked first.** `_handle_registration`'s check
order becomes: required-fields → signature → canonicalize key →
`resolve_identity` → `enforce_not_banned` → `enforce_identity_cap` →
`register`. Ban is checked before the cap because it's the more
fundamental rejection reason — an operator or volunteer reading logs
should see "you're banned," not "you're at capacity," when both happen
to be true. Unlike `enforce_identity_cap`, `enforce_not_banned` has *no*
exemption for a public key that's already registered: the cap's
exemption exists because a reconnect isn't consuming a *new* slot, but a
ban must reject every future registration attempt from a banned
identity, reconnect or brand-new key alike — that's the literal
acceptance criterion. A brand-new public key from a banned identity still
costs a real GitHub verification call before `enforce_not_banned` catches
it, for the same structural reason #35's cap-rejected keys do: identity
resolution has to happen before either check can know which identity it's
looking at. Accepted the same way #35 already accepted it — not a new
problem this ticket introduces.

**New exceptions: `UnknownIdentity`, `IdentityBanned`.** Matches the
established plain-`Exception`-subclass, docstring-explained style already
used for `MissingGithubToken` and `IdentityCapReached` — no new pattern
introduced. `UnknownIdentity` is raised by `ban_identity` (operator asked
to ban a login the coordinator has never seen); `IdentityBanned` is
raised by `enforce_not_banned` (a registration attempt from an already-
banned identity).

**New CLI: `mycelium-coordinator-ban --identity <login>`, reusing
`mycelium-coordinator-status`'s exact connection pattern and shared
token.** Same `--coordinator-url`/`--coordinator-cert`/`--token-file`
flags, same one-shot connect/send/receive/close shape, same shared
operator token via `registry.check_token` — not a new, stronger
authentication boundary. This isn't a new decision so much as following
existing precedent to its logical conclusion: `mycelium-coordinator-status`
(a read-only operator tool) already gates on the exact same shared token
every client also holds, and the PRD already frames that token as *the
operator's* token, clients being handed a copy of it. A ban command is
squarely within "things the operator's own tooling does," matching
`mycelium-coordinator-status`'s own trust boundary rather than inventing
a separate one.

**New wire message: `{"type": "ban_identity", "token", "identity"}` →
`{"type": "banned", "identity", "disconnected_count"}` or `{"type":
"ban_failed", "reason"}`.** `disconnected_count` gives the operator
direct confirmation their command actually reached any live connections,
not just that it was accepted — meaningful feedback for AC3's "already-
connected nodes are disconnected" guarantee, not just AC1's "operator can
revoke an identity." Reuses the existing `registration_rejected` message
type (unchanged) for the separate, already-covered case of a *node*
being rejected at registration time because its identity is banned — the
new `banned`/`ban_failed` pair is specifically the ban *command's own*
response, a different exchange entirely.

## Explicitly out of scope for this issue

Any unban/appeal mechanism (see above — a coordinator restart is the only
reset path in this phase). Pre-emptively banning a GitHub login that has
never registered a node. Any change to `mycelium-coordinator-status`'s
output to show which identities are currently banned — not asked for by
any of this issue's acceptance criteria. Persisting ban state across a
coordinator restart — matches every other piece of in-memory-only
coordinator state. Any interaction with #35's per-identity cap or #36's
reputation counters beyond check ordering — unrelated concerns, no shared
state or logic.
