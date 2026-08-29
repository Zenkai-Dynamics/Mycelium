# Issue #48 — Register Mycelium's Real GitHub App — Design

Date: 2026-08-29
Status: Approved, not yet executed
Issue: [#48 — Register Mycelium's real GitHub App (swap the placeholder CLIENT_ID)](https://github.com/Zenkai-Dynamics/Mycelium/issues/48)
Related: [issue #49 — Live-hardware + real-GitHub verification](https://github.com/Zenkai-Dynamics/Mycelium/issues/49)
(blocked by this ticket), [issue #34's design doc](2026-08-27-issue-34-device-flow-signin-design.md)
(established GitHub App + device flow as the chosen mechanism)

This is the condensed record of the decisions made while brainstorming/
grilling issue #48. Unlike every other Phase 1 ticket, this one has
almost no code in it — the actual registration happens in GitHub's web
UI, under the operator's own account, and no agent can do that step.
This doc exists so the *reasoning* behind each registration-form choice
isn't lost (several are effectively permanent — see below), and so the
operator has a precise walkthrough rather than a blank form.

## What issue #48 asks for

A real GitHub App exists with Device Flow enabled and "Expire user access
tokens" turned off; its `client_id` replaces the placeholder in
`src/mycelium/node/github_device_flow.py`'s `CLIENT_ID` constant; running
`mycelium-node` with no cached token against that real `client_id`
successfully prints a device code and verification URL.

## Decisions made

**Ownership: the Zenkai-Dynamics org, not the operator's personal
account.** Confirmed the operator holds sufficient rights (Owner or
"GitHub App manager") on that org to register there. This matters more
than it looks: a GitHub App's identity is its `client_id`, embedded
directly in `mycelium-node`'s source (per #34's design — no env-var
override, "ships embedded"). Every volunteer who has ever completed the
device flow has effectively trusted that specific `client_id`. Migrating
ownership later means registering a *new* App with a *new* `client_id`,
which means every existing volunteer has to re-authorize from scratch —
there's no in-place ownership transfer that preserves continuity for this
use case. Tying the App to one person's personal account was rejected as
a durability risk: if that person leaves the project or loses account
access, a piece of production authentication infrastructure goes with
them. Org ownership avoids that without costing anything today.

**App name: "Zenkai Mycelium,"** with a decided fallback order if taken
(GitHub App names are globally unique across all of GitHub, not just
within the org): Zenkai Mycelium → Zenkai Mycelium Network → Mycelium
Node Network → Zenkai-Dynamics Mycelium. This is the string a volunteer
sees on GitHub's "Authorize \<name\>" consent screen before they type in
their device code — it needs to read as legitimate, not like a
placeholder or test app, and needs to survive a name-availability check
that can't be resolved from documentation alone (an attempted uniqueness
check via `github.com/apps/zenkai-mycelium` returned a 404, which is weak
evidence the name is free but not reliable enough to trust outright — a
fetch-tool-level block looks identical to a true "doesn't exist," so this
is confirmed live on the form, not assumed here).

**Homepage URL: `https://github.com/Zenkai-Dynamics/Mycelium`** (the repo
itself). A required field with no real ambiguity and no real cost if it
turned out to need changing later — unlike ownership/naming, not treated
as consequential enough to warrant alternatives.

**Permissions: everything left at "No access," with an explicit
no-pre-decided-fallback.** The coordinator's only call to the GitHub API
is `GET /user` (see `coordinator/github_identity.py`, unchanged by this
ticket), which returns the authenticated user's own basic profile (`id`,
`login`) without requiring any granted permission — a volunteer
authorizing this App should see it requesting nothing beyond confirming
who they are. Whether GitHub's registration form actually accepts
saving with zero permissions selected could not be confirmed from
documentation (one fetch returned "no indication that zero permissions
is acceptable" — inconclusive, not a documented requirement either way).
Rather than guess a fallback permission now — which would materially
change what a volunteer sees themselves granting — the decision is to
attempt "No access" everywhere first and, if the form actually blocks
that, stop and decide the fallback together with real information about
what GitHub's UI is actually asking for.

**Installation scope: "Only on this account" (Zenkai-Dynamics), plus a
new mandatory verification step this decision didn't have before
grilling.** Nobody needs to *install* this App on any repository — it
exists purely to let a volunteer *authorize* it via device flow and
obtain an identity token, a functionally separate GitHub concept from
installation (confirmed: "You can authorize the app without installing
the app," per GitHub's own docs). Restricting installability to the
owning org costs nothing since public installability was never going to
be used. The risk that got surfaced during grilling: whether an
installation-scope restriction *also* gates who can complete device-flow
*authorization* could not be confirmed to a documentation-airtight degree
across five separate fetch attempts against `docs.github.com` — every
page consistently described installation and authorization as
independent operations without ever explicitly stating "an app owner can
restrict installation while authorization stays open to any GitHub
user." Getting this wrong would be silently catastrophic: Phase 1 exists
specifically to open the node pool to public, non-org volunteers, and a
scope restriction that blocked non-org authorization would look
completely fine in the operator's own testing (the operator *is* in the
org) while quietly failing for every actual external volunteer.
Switching to "Any account" (public installability) to sidestep the
question entirely was considered and rejected: it doesn't remove any real
risk (nothing in these docs suggested a *public* app has different
authorization behavior than a private one; the entire premise being
tested is whether installation-scope affects authorization at all,
independent of public/private), it only removes the *feeling* of risk
while adding unnecessary public-installability surface for a capability
never used. Instead: **before #48 is considered done, the operator tests
device-flow authorization from a GitHub account with zero relationship
to Zenkai-Dynamics** (a personal or alt account, not an org member) —
cheap, takes minutes, and settles the question with real evidence rather
than trusting documentation that never fully committed either way. This
is now a new, explicit acceptance criterion (see below), not implied by
the original ticket text.

**Callback URL: left blank.** Confirmed via GitHub's docs: this field is
explicitly ignored when an App uses device flow instead of the web
application flow, so leaving it blank has no downstream effect on
anything this project does.

**Webhooks: left inactive.** Nothing in Mycelium's coordinator or node
agent listens for or reacts to GitHub webhook events — activating this
would be pure unused surface area.

**"Expire user access tokens": deselected during initial registration,
not as a follow-up settings change.** Confirmed via GitHub's docs that
this checkbox is on the App-creation form itself (step 10 of a 22-step
flow), not something only exposed after the App already exists — so the
walkthrough tells the operator to handle it inline during the one pass
through the form, rather than requiring a second trip to a settings page
afterward. Left selected (GitHub's default), the App's device-flow
tokens would expire on GitHub's standard 8-hour cycle — `docs/OPERATIONS.md`
already documents why that's specifically bad here: it would turn every
coordinator restart into a forced fresh GitHub sign-in for every
currently-registered volunteer, not just a free reconnect.

**Not re-litigated (settled during issue #34):** GitHub App, not a
legacy OAuth App — specifically because OAuth Apps' device-flow tokens
don't have the "Expire user access tokens" opt-out that GitHub Apps have.
Device flow enabled. Empty OAuth scope requested at authorization time
(a `github_device_flow.py` code-level concern, distinct from the
registration form's "Permissions" section, which governs API access
scope rather than OAuth scope — these are two different GitHub
mechanisms that happen to sound similar).

## Deliverable: what actually happens next

No plan doc follows this design doc — the "implementation" is a
step-by-step walkthrough for the operator to execute in GitHub's web UI
(everything above, in order, matching GitHub's actual 22-step form),
followed by the operator handing the resulting `client_id` back so a
one-line commit can replace the placeholder in
`src/mycelium/node/github_device_flow.py`. There is no code for an
implementer subagent to write beyond that single-constant swap, and no
test suite exercises a real `client_id` (per #34's design — tests use
injected fakes) — real-world exercise of the swapped-in value is #49's
job, not this ticket's.

## Acceptance criteria (supersedes the original issue text with the
grilling-surfaced addition)

- [ ] A real GitHub App named "Zenkai Mycelium" (or the next available
  fallback name) exists under the Zenkai-Dynamics org, with Device Flow
  enabled
- [ ] "Expire user authorization tokens" is deselected
- [ ] Permissions are at "No access" (or whatever minimal fallback was
  jointly decided live, if the form required one)
- [ ] Installation scope is "Only on this account"
- [ ] **New**: device-flow authorization is tested and confirmed
  successful from a GitHub account with no relationship to
  Zenkai-Dynamics org — not just the operator's own account
- [ ] The App's `client_id` replaces the placeholder in
  `src/mycelium/node/github_device_flow.py`'s `CLIENT_ID` constant,
  committed
- [ ] `mycelium-node` run with no cached token against this real
  `client_id` prints a device code and verification URL

## Explicitly out of scope for this issue

Full end-to-end registration against a real coordinator (that's #49).
Any change to what data the coordinator requests or stores (`GET /user`
only, unchanged — this ticket is registration, not a protocol change).
An env-var override for `CLIENT_ID` (already rejected during #34).
Deciding a specific permissions-form fallback in advance (deliberately
left open — see above).
