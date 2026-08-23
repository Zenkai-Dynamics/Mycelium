# Issue #39 — GitHub OAuth Identity Verification Gates Node Registration — Design

Date: 2026-08-23
Status: Implemented
Issue: [#39 — GitHub OAuth identity verification gates node registration](https://github.com/Zenkai-Dynamics/Mycelium/issues/39)
Parent: [#31 — Phase 1: Open node pool to public volunteers](https://github.com/Zenkai-Dynamics/Mycelium/issues/31) —
see [the Phase 1 design doc](2026-08-23-phase-1-open-network-design.md) for
the full phase-level rationale this ticket implements one slice of.

This is the condensed record of the decisions made while brainstorming/
grilling issue #39, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
the Phase 0 issue design docs and
[issue #33's design doc](2026-08-23-issue-33-node-keypair-identity-design.md).

## What issue #39 asks for

Replace Phase 0's shared-token gate (kept as a placeholder by #33) with real
GitHub-based trust: a node's registration message carries a GitHub OAuth
access token on its *first* registration, verified via a new injectable
identity-verifier interface (real implementation calls GitHub's `GET /user`
once). The coordinator persists the resulting identity against the node's
public key (from #33) — never storing the raw GitHub token long-term. A
node reconnecting with an already-known public key does not need to resupply
a GitHub token. This ticket does not include the self-service CLI flow that
obtains the GitHub token for the operator (that's #34) — for this ticket,
the token can be supplied by any means (e.g. obtained by hand).

## Decisions made

**Wire protocol: `token` is removed from node registration, replaced by an
optional `github_token`.** Client-side auth (`complete`/`status_query`)
is untouched — only `register` changes, and only for nodes. `github_token`
is optional on the wire because the node itself can't know whether the
coordinator already recognizes its public key; it just forwards whatever
it has (or nothing) and lets the coordinator authoritatively decide whether
that was necessary. Check order in `_handle_registration` becomes:
required-fields → signature verification → canonicalize key → identity
check — identity check moves to where the old token check used to sit,
deliberately *after* signature verification, so an unauthenticated probe
with a spoofed public key never triggers a GitHub API call.

**Identity check has exactly two outcomes, encapsulated in one new
`NodeRegistry` method.** If the public key is already bound to an identity
(recognized from an earlier registration this coordinator process has
seen), registration proceeds immediately and any `github_token` sent is
silently ignored — no re-verification, ever, on a reconnect. Re-verifying
on every reconnect was considered and rejected: issue #39's own acceptance
criteria requires reconnects to need no GitHub token at all, and demanding
one to also *match* the existing binding would mean a GitHub API call on
every reconnect, exactly what that acceptance criteria rules out. If the
public key is unbound, `github_token` is required; its absence or GitHub's
rejection of it are two distinct failure modes (see below), and success
binds the resulting identity and proceeds.

**Two distinct rejection reasons, not one.** `"github_token is required for
first-time registration"` when the field is simply missing on an unbound
key. For a *supplied* token, GitHub's own verdict is split further:
`"invalid or expired GitHub token"` for a real 401/403 from GitHub (the
volunteer's problem), versus `"could not reach GitHub to verify identity,
try again"` for anything else — timeout, network error, an unexpected
status code (the coordinator's/network's problem). Collapsing these two
into one generic reason was considered — rejected, since telling a
volunteer their token is bad when GitHub was simply unreachable is
misleading and produces a support/confusion cost with no offsetting
benefit (this isn't a security-sensitive distinction worth hiding from a
prober — an attacker learns nothing useful from which of the two strings
comes back). All three reasons reuse the existing `registration_rejected`
message type, matching #33's own precedent.

**Identity verifier: a plain injectable async function, not a class
interface.** `verify_identity(github_token) -> GithubIdentity` lives in a
new `mycelium/coordinator/github_identity.py`, raising
`IdentityVerificationError` (itself distinguishing the 401/403 case from
everything else, so `_handle_registration` can map to the two rejection
reasons above) on failure. Injected into `NodeRegistry`'s constructor as
`identity_verifier`, the same shape `check_token` already has as a
registry-owned concern — a class-based verifier interface was considered
and rejected as unnecessary ceremony for a single-method seam; Python
callables are already injectable. Real implementation uses stdlib
`urllib.request` wrapped in `asyncio.to_thread(...)`, the same
blocking-call-off-the-event-loop pattern `node/cli.py` already uses for
vLLM's `start`/`wait_ready` — no new dependency for one GET call. Timeout:
5 seconds on the `urlopen` call, which is also why node-side
`REGISTRATION_TIMEOUT_SECONDS` moves from 10.0s to 15.0s (comfortable
headroom over the new call's worst case plus existing overhead). Both are
plain config constants, not architectural commitments.

**`GithubIdentity` carries `id` and `login`, not just an opaque
identity_id.** The issue's own framing is `verify_identity(token) ->
identity_id`; this ticket returns both fields from the same `GET /user`
response instead of discarding one. `id` (GitHub's stable numeric user id,
immutable across username renames) is the actual identity key #35's cap
and #37's ban will index on. `login` (the GitHub username at bind time) is
display-only, surfaced by `mycelium-coordinator-status` so an operator can
recognize *who* a node belongs to instead of auditing a bare number — it
can go stale if a volunteer renames their GitHub account later, which is a
cosmetic-only concern, not a security one, since nothing keys off it.

**Persistence: in-memory only, matching every other piece of coordinator
state.** `NodeRegistry` gains a `public_key -> GithubIdentity` map with no
backing store, exactly like the connection registry itself. A coordinator
restart forgets every binding, the same way it already forgets every
registered node — a volunteer's next connection after a restart just goes
through the unbound-key path again. Building durable storage (a JSON file,
sqlite, anything) was considered and rejected: nothing in this repo
persists coordinator state today, none of #39's seven acceptance criteria
require surviving a restart, and adding new persistence infrastructure here
would also obligate #35 and #37 to decide how their own state interacts
with it — a materially larger scope increase than this ticket asks for.

**This limitation is sharper than "just resupply the file," and is
documented as such.** GitHub OAuth Apps default to *"Expire user access
tokens"* on — device-flow tokens expire after 8 hours, with a 6-month
refresh token. Combined with in-memory-only persistence, a volunteer's
cached token is quite likely already expired by the time any coordinator
restart happens, so a restart can force a full fresh GitHub sign-in, not
just a cheap resupply-the-same-token reconnect. No code in this ticket
changes as a result, but `docs/OPERATIONS.md`/`docs/phases/
phase-1-open-network.md` state this plainly, and record a recommendation
for whoever creates Mycelium's GitHub OAuth App in #34: turn *off* "Expire
user access tokens" at app-creation time, so real volunteer tokens don't
carry this expiry at all. That's a one-time dashboard setting on GitHub's
side with zero code impact on this ticket.

**`mycelium-node` CLI: `--token-file` removed, `--github-token-file`
added.** Same read/strip/empty-check pattern as the flag it replaces.
Always safe to keep supplying it on every run — the coordinator ignores it
once a key is already bound, so there's no "remember to drop the flag
later" footgun to document or get wrong. No dual-mode/backward-compat
shim, matching every prior Phase 0/#33 ticket's precedent for this
pre-1.0, single-operator-controlled software.

**`mycelium-coordinator-status` shows the bound `login`.** `list_nodes()`
gains an `identity` field; output becomes e.g.
`nodeA [a1b2c3d4e5f6] (github:octocat): Qwen/Qwen2.5-7B-Instruct`.

**Testing: fake `identity_verifier` for all coordinator-side registration
tests, no real GitHub calls.** A canned-success / raises-
`IdentityVerificationError` fake injected via `NodeRegistry`'s constructor
parameter, matching how #33's tests never spin up real crypto edge cases
beyond what's needed. The real `github_identity.verify_identity` gets its
own small test module that monkeypatches `urllib.request.urlopen` at the
boundary to exercise its parsing/error-mapping (401 vs. other failures)
without a real network call. Integration tests cover all four registration
outcomes: new key + valid token succeeds; reconnect with no token succeeds
once bound; new key + missing token is rejected with the specific reason;
new key + invalid token is rejected with the specific reason.

## Explicitly out of scope for this issue

The self-service device-flow sign-in CLI that obtains the GitHub token
automatically (#34) — this ticket assumes the token is supplied by hand
through `--github-token-file`. Per-identity registration caps (#35).
Reputation tracking/weighting (#36). Manual ban (#37). Any durable/
cross-restart persistence of the identity binding (see above — deliberately
deferred, not silently forgotten). Any change to client-side auth
(`complete`/`status_query` keep using the shared token exactly as today).
Support for GitHub Enterprise Server or any non-`api.github.com` endpoint —
the verifier calls `https://api.github.com/user` unconditionally, matching
the issue's GitHub-OAuth-only scope for Phase 1.
