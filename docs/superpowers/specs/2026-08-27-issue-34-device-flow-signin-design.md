# Issue #34 — Node-Side GitHub Device-Flow Sign-In — Design

Date: 2026-08-27
Status: Approved, not yet implemented
Issue: [#34 — mycelium-node drives the GitHub device-flow sign-in itself](https://github.com/Zenkai-Dynamics/Mycelium/issues/34)
Parent: [#31 — Phase 1: Open node pool to public volunteers](https://github.com/Zenkai-Dynamics/Mycelium/issues/31) —
see [the Phase 1 design doc](2026-08-23-phase-1-open-network-design.md) for
the full phase-level rationale this ticket implements one slice of.

This is the condensed record of the decisions made while brainstorming/
grilling issue #34, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established by
the Phase 0 issue design docs and [issue #37's design
doc](2026-08-25-issue-37-manual-node-ban-design.md).

## What issue #34 asks for

Running `mycelium-node` with no pre-obtained GitHub token drives the full
OAuth device-flow sign-in itself, directly against GitHub's own
device-code/token endpoints — no coordinator involvement in the OAuth
dance, no local browser or callback listener required on the node. It shows
a code, tells the operator to visit `github.com/login/device` (on any
device with a browser, not necessarily the node itself), polls until
authorized, then registers using the resulting token via #39's
already-working coordinator-side verification (`github_token` on the
`register` message). The GitHub OAuth App/GitHub App's `client_id` is not
secret and ships embedded in the `mycelium-node` package. Node-side tests
use an injectable device-flow-client seam with a fake implementation — no
real network calls to GitHub in the test suite.

## Decisions made

**`client_id`: a placeholder constant, swapped in later by the operator.**
`src/mycelium/node/github_device_flow.py` defines `CLIENT_ID` as an obvious
placeholder sentinel (e.g. `"REPLACE_ME_WITH_REAL_GITHUB_APP_CLIENT_ID"`),
not an environment-variable override. Registering the real GitHub App is an
out-of-band human action against the operator's own GitHub account/org —
nothing an agent implementing this ticket can do — and per the issue text
the value "ships embedded," not configurable. `docs/OPERATIONS.md` already
carries a forward-note that the real App should have "Expire user access
tokens" turned off; that registration step happens after this ticket lands,
as a follow-up, using the real client_id in a small commit that only
touches this one constant.

**Fail fast if `CLIENT_ID` is still the placeholder.** Before making any
network call, `github_device_flow.request_device_code()` checks `CLIENT_ID`
against the placeholder sentinel and raises a clear, specific error
("mycelium-node's GitHub App is not configured yet — see
docs/OPERATIONS.md") if it matches. Without this, a not-yet-configured
deployment would print a device code, start polling, and only fail deep in
the loop with GitHub's opaque `incorrect_client_credentials` — a
config-not-done problem masquerading as a runtime failure. Cheap to add,
turns a confusing failure into an obvious one.

**Token caching: `--github-token-file` gets a default path, doubling as an
auto-managed cache.** `--github-token-file` now defaults to
`~/.mycelium/github-token` (mirroring `--node-key-file`'s existing default
of `~/.mycelium/node-key.pem`). If the file exists, it's read and used
verbatim — device flow never runs, identical to today's behavior with an
explicit `--github-token-file`. If it doesn't exist, the device flow runs,
and the resulting token is written to that path (`chmod 600`) before
registering, so a process restart on the same node doesn't force the
volunteer to re-authenticate. This is `identity.load_or_create_keypair`'s
generate-if-missing idiom applied to a token instead of a keypair — same
codebase pattern, not a new one. One flag now serves both "operator
hand-supplied token" and "auto-managed cache" purposes; since the write
path only triggers when the file is *absent*, a manually-populated token
file (the existing `gh auth token` / PAT workflow `docs/OPERATIONS.md`
already documents) is never touched or overwritten by this code.

**Explicit vs. default path: only the true default falls through to device
flow.** `--github-token-file`'s argparse default becomes `None`, resolved
against `DEFAULT_GITHUB_TOKEN_PATH` inside `_run()` so the code can tell
"operator explicitly passed a path" apart from "operator passed nothing."
An explicitly-passed path that doesn't exist is a hard, immediate
`SystemExit` with a clear message — not a fallback into device flow. Without
this distinction, a typo'd `--github-token-file` path on an unattended
headless box would silently start an interactive device-flow prompt nobody
is watching for, hanging the node indefinitely instead of failing loudly at
startup the way a bad path should.

**Sequencing: device flow slots into the same pre-vLLM-start position the
token-file read already occupies today — it doesn't move anything later.**
Checked against the actual code, not assumed: `_run()` already reads
`--github-token-file` (when given) *before* `process.start()` — the
`node_id`/keypair/`github_token` setup block sits entirely above the
`"starting vLLM..."` print and the `await asyncio.to_thread(process.start)`
call that follows it. So this ticket isn't reordering existing structure;
it's replacing the current `else:` case (no file given → `github_token`
stays `None`, silently deferred to the coordinator's
`"github_token is required"` rejection) with the interactive device flow,
in the exact same spot. That placement matters more now than it did before,
because unlike an instant file read, the new interactive polling can take
real wall-clock time — and it's only ever triggered on a genuinely
first-ever run for a given node (no cached token, no hand-supplied file),
exactly the moment a volunteer is actively watching the terminal to
complete the step. Keeping it pre-vLLM-start means the code + instructions
appear the instant `mycelium-node` launches rather than after a
multi-minute boot log, and an `expired_token` retry (see below) costs
nothing instead of wasting an already-completed vLLM boot. Confirmed safe:
`VLLMProcess.stop()` is already a no-op if `start()` was never called
(checks `self._process is None`), so the existing raw-signal Ctrl+C handler
behaves correctly even if interrupted mid-poll, before vLLM ever starts.

**Polling loop mechanics (RFC 8628 device-flow semantics, verified against
GitHub's own docs, not assumed):**
- `authorization_pending` → keep polling at the current interval.
- `slow_down` → GitHub's response includes the new interval to use;
  the client uses that value directly rather than hand-computing `+= 5`.
- `expired_token` → transparently request a fresh device code, print the
  new code/URL, and keep looping. Cheap and justified by the sequencing
  decision above — no vLLM boot is ever thrown away.
- `access_denied` → exit the process immediately, telling the operator to
  re-run `mycelium-node` to try again. GitHub's own client guidance for
  this error ("the code can't be reused, request a new one") is phrased as
  a constraint on the *code*, not a mandate to keep prompting the *human*;
  treating an explicit "Cancel" click as informed consent to stop is the
  less surprising of the two readings.
- Any other error code (`unsupported_grant_type`,
  `incorrect_client_credentials`, `incorrect_device_code`,
  `device_flow_disabled`, or anything unrecognized) → catch-all fatal exit,
  printing the raw error. These are configuration/programming-bug signals,
  not transient states — retrying forever would never self-resolve them,
  unlike the four cases above which are all legitimate points in a normal
  flow.

**Scope requested: none.** GitHub's device-code request's `scope` parameter
is optional (confirmed against GitHub's docs, not assumed) and
`docs/OPERATIONS.md` already documents that "no scopes are required, since
only `GET /user` is called" for the manual-PAT path — the device-flow path
inherits the same reasoning.

**Stale/rejected cached token: still retries forever like every other
rejection reason, but with a distinguishing message.** The coordinator's
`registration_rejected` reason `"invalid or expired GitHub token"` is not
programmatically special-cased into new control flow — the node's existing
`RegistrationError` handling (log + exponential backoff, forever) stays
exactly as it is for every other reason (banned, cap-reached, malformed
request). The one addition: when the rejection reason string matches this
specific case, the printed message adds a distinguishing hint — `"...this
looks like a stale GitHub token — delete ~/.mycelium/github-token to
re-authenticate"` — instead of the fully generic `"registration failed:
{exc}; retrying in {delay}s"`. This isn't auto-recovery and adds no
structural coupling beyond one string match; it exists because caching
(this ticket) turns a failure mode that used to require active operator
involvement to even occur (they'd have had to hand-create a bad token file)
into one that can happen silently, automatically, and invisibly on an
unattended volunteer box weeks after setup — a plain diagnostic hint costs
nothing and meaningfully shortens time-to-fix for exactly the case caching
makes more likely.

**Seam shape: a duck-typed client object, not a formal ABC/Protocol.**
`github_device_flow.py` exposes `request_device_code()` and
`poll_once(device_code)` as free functions (the real implementation, doing
blocking HTTP calls). The orchestration loop — living in `node/cli.py` as
`_authenticate(client=github_device_flow, sleep=asyncio.sleep)` — calls
`client.request_device_code()` and `client.poll_once(...)` via
`asyncio.to_thread`, and `await sleep(interval)` between polls. In
production, `client` defaults to the real module; tests inject a small fake
object exposing the same two methods, plus a no-op `sleep` stub, so no test
ever makes a real network call or waits in real time. This exactly mirrors
`NodeRegistry`'s existing `identity_verifier` injection convention from
issue #39 (a constructor/param-injected callable defaulting to the real
implementation) — no new DI pattern introduced. Orchestration lives in
`cli.py`, not the new module, because it needs `asyncio.sleep` between
polls and progress printing — matching how `_run()` already owns
`node/cli.py`'s top-level control flow rather than pushing it into leaf
modules.

**Best-effort browser auto-open.** On top of always printing the code and
verification URL (the guaranteed-working, headless-safe path), `_authenticate`
also attempts `webbrowser.open(verification_uri)`, wrapped in a broad
`try/except` so a headless box without a display never surfaces an error
from this — it's a pure convenience for a volunteer running `mycelium-node`
on their own desktop, not a requirement any acceptance criterion depends
on.

## Testing approach

- `tests/node/test_github_device_flow.py` — direct unit tests of the real
  `request_device_code`/`poll_once` implementations, mocking
  `urllib.request.urlopen` the same way `tests/coordinator/test_github_identity.py`
  tests `github_identity._fetch_user` directly (confirmed against that
  file, not assumed) — including the placeholder-`CLIENT_ID` fail-fast
  check and each of the five polling-error branches (`authorization_pending`
  and success aren't errors; `slow_down`, `expired_token`, `access_denied`,
  and the catch-all fatal case are).
- `tests/node/test_cli.py` — extends existing coverage with `_authenticate`'s
  full polling loop (pending → success; slow_down honoring the server's
  interval; expired_token re-requesting a code; access_denied exiting) and
  the cache read/write-back logic (default path absent → device flow runs,
  token written; default path present → read verbatim, device flow never
  invoked; explicit path absent → hard `SystemExit`), using the fake client
  and no-op `sleep` — no real network calls or real waiting anywhere in the
  suite.

## Documentation updates

`docs/OPERATIONS.md` Step 3 gets rewritten: the no-token case now runs
automatically (device flow), replacing the current "Until issue #34 adds a
built-in device-flow sign-in, obtain one by hand..." forward-reference. The
existing manual `gh auth token` / PAT workflow stays documented as a valid
alternative — it still works unchanged, since it's just pre-populating the
same file the automatic flow would otherwise create. A short operator-facing
note flags that the shipped `CLIENT_ID` is a placeholder until the real
GitHub App is registered, as a follow-up action outside this ticket's scope.

## Explicitly out of scope for this issue

Registering the real GitHub App itself (an out-of-band operator action,
follow-up to this ticket). An environment-variable override for
`CLIENT_ID` — the issue text describes it as embedded, not configurable.
Auto-detecting and recovering from a stale cached token beyond the
diagnostic message described above — no auto-delete, no auto-restart of the
device flow from inside the registration-retry loop. Any change to the
`--prompt`-only standalone mode (device flow is skipped entirely in that
path today and stays that way — it never touches the coordinator). Any
change to coordinator-side behavior — #39 already accepts a `github_token`
on `register`, unchanged by this ticket.
