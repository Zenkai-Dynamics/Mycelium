# Issue #68 — Operator-Facing Requests Share the Client Token — Design

Date: 2026-10-10
Status: Approved and implemented (docs/OPERATIONS.md, status_cli.py,
ban_cli.py, registry.py — see issue #57's design doc, which needed this
done first)
Related: [issue #63's design](2026-09-05-issue-63-exposure-handles-design.md)
(the whole-branch review that surfaced this gap), [issue #68](https://github.com/Zenkai-Dynamics/Mycelium/issues/68),
[PRD: issue #54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54)

## The problem

`coordinator/server.py` gates three message types — `complete`, `status_query`,
`ban_identity` — through the same `registry.check_token()`, backed by one
shared secret file (`--token-file`). `status_query` returns every registered
node's fingerprint and bound GitHub login; `ban_identity` disconnects and
bans an identity outright. Any holder of the ordinary client token can
therefore enumerate every volunteer's real-world identity and unilaterally
ban one, with no separate credential standing in the way.

## Decision

**Don't split the credential.** The token stays single, shared by `complete`,
`status_query`, and `ban_identity` alike — but it is now documented,
explicitly and consistently, as an operator credential rather than an
ordinary client secret.

This matches the deployment model already decided elsewhere in this
project, not a new assumption invented for this issue:

- `docs/phases/phase-1-open-network.md` already calls it "a single shared
  operator token" when describing Phase 1's scope.
- Phase 2's non-goals explicitly list "Opening client-side access to the
  public (inherited from Phase 1)" — there is no population of
  non-operator client-token holders in this phase's scope to defend
  against.
- `status_cli.py`'s own module docstring already calls itself "an operator
  tool, not a node agent" — the code's self-description already agrees
  with this decision; only `OPERATIONS.md`'s prose lagged behind it.
- Confirmed directly with the operator: no near-term plan to hand the
  client token to anyone but themselves.

A second `--operator-token-file` gating `status_query`/`ban_identity` was
considered and rejected: it adds a secret every single-operator deployment
must generate, store, and pass to two more CLIs, to defend against a client
population that does not exist in this phase. That is exactly the shape of
speculative complexity CLAUDE.md's Simplicity First guidance flags.

`ban_identity`'s asymmetric risk (worse than enumeration, per the issue's
own text) was considered on its own — a confirm-again-by-typing-the-identity
prompt, or an audit log — and rejected for this issue: both are real
features with their own design questions (does confirmation break
scripting? where do logs live?) that #68 didn't ask for. Documentation is
judged sufficient.

## Revisit trigger

This decision holds only as long as the client token has exactly one
holder: the operator. **Revisit the split the moment either stops being
true** — client-side access opens to the public (reversing Phase 1/2's
non-goal), or the operator hands the token to anyone else for any reason.
Either condition reintroduces the population this decision assumes doesn't
exist.

## Scope

- `docs/OPERATIONS.md`, Step 1 — extend the existing warning ("Anyone who
  has it can submit completions and ban any node's identity") to also name
  identity enumeration via `status_query`. State plainly that this is an
  operator credential and must not be given to an untrusted client.
  Standardize on the term "operator token" here.
- `src/mycelium/coordinator/status_cli.py`, `src/mycelium/coordinator/ban_cli.py`
  — module docstrings currently say "same shared token"; reworded to
  "operator token" for consistency. Prose only; the `--token-file` flag
  itself is unchanged (no `help=` text exists to update, and no test pins
  this flag's name or description).
- `src/mycelium/coordinator/registry.py` — the comment calling it "the
  shared token" updated to match, for the same consistency reason.
  `NodeRegistry`'s actual field/parameter names (`token`, `_token`) are
  unchanged — this is a renaming of prose, not of the API.

## Out of scope

A separate operator credential (see Decision above). Any confirm-prompt or
audit-log guard on `ban_identity` (see Decision above). Renaming the
`--token-file` CLI flag or the `token`/`_token` identifiers in code — only
the English description of what the token *is* changes. #57's privacy
documentation, which depends on this decision but is its own ticket.
