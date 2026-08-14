# Issue #4 — Node Network Reachability — Design

Date: 2026-08-14
Status: Approved, evidence gathered, not yet written up
Issue: [#4 — Investigate & document real node network reachability](https://github.com/Zenkai-Dynamics/Mycelium/issues/4)

This is the condensed record of the decisions made while brainstorming/
grilling issue #4, plus the live-test evidence gathered during design —
before the ADR and doc updates are written. It exists so the *reasoning*
behind the transport decision isn't lost, per the pattern established in
the [issue #1](2026-08-12-issue-1-project-skeleton-design.md),
[issue #2](2026-08-14-issue-2-dependency-pinning-design.md), and
[issue #3](2026-08-14-issue-3-developer-setup-guide-design.md) design
docs.

## What issue #4 asks for

Resolve `docs/phases/phase-0-foundation.md`'s "Node network reachability
is unconfirmed" open risk: can a coordinator dial into a candidate node,
or does the node need to dial out and hold a connection open (BOINC/
ngrok/torrent-tracker pattern)? Must be a real spike against real target
machines, not a guess. A decision — which side initiates, and why — gets
written down, and the phase doc's open-risk entry gets updated to reflect
it.

## Candidate nodes identified

Two architecturally distinct environments, both named in the operator's
SSH config and matching the phase doc's "CDAC HPC allocation" /
"university-VPN-gated lab machine" language:

- **`a6000` / `h100`** (192.168.22.23, 192.168.3.215) — IIITD lab GPU
  machines, private RFC1918 addresses, reachable only while the
  operator's machine has an active university VPN tunnel
  (`vpn.iiitd.edu.in`, confirmed via `scutil --nc status` — FortiClient
  SSL VPN).
- **`paramrudra`** (`paramrudra.iuac.res.in`, SSH port 4422) — an IUAC/
  CDAC HPC cluster login node, a **public** hostname/IP
  (`14.139.62.247`), reachable without the VPN.

Both confirmed still live Phase 0 candidates by the operator.

## Decisions made

**Scope: test both environments, not just one.** They represent
genuinely different network postures (private-IP-behind-VPN vs.
public-IP-behind-institutional-firewall), and the transport decision
needs to generalize across all of Phase 0's real nodes, not just
whichever one happened to be tested first. Both were already accessible,
so the marginal cost of testing both was small.

**Test methodology: a genuinely external vantage point, not the
operator's own VPN-tunneled connection.** Early in the investigation,
`scutil`/`netstat` showed the operator's Mac had an *active, full-tunnel*
VPN connection (default route via `utun6`) at the moment testing began —
meaning any "public IP" check run without accounting for this would have
silently tested from *inside* the university network, invalidating the
result. The VPN was deliberately disconnected (with the operator's
explicit consent) for the external portion of the test, then reconnected
immediately after. This is the same reasoning [issue #3](2026-08-14-issue-3-developer-setup-guide-design.md)'s "verify for
real, don't assume" pattern already established — a claim about network
reachability needs to be tested from the actual vantage point the claim
is about.

**No live test needed for `a6000`/`h100` beyond confirming the obvious.**
A private RFC1918 address (192.168.0.0/16) is categorically unroutable
from the public internet — this is a networking fact, not something a
port scan proves or disproves. It was confirmed directly anyway (`nc -zv`
to `192.168.22.23:22` timed out once the VPN was disconnected), but the
real content of the decision for these nodes is the addressing scheme,
not a firewall configuration that could change.

**Live arbitrary-port test for `paramrudra`, run by the operator +
verified end-to-end.** Because the tool-use environment's automated
permission classifier blocked starting a background listener on a remote
host directly, the operator started `python3 -m http.server 8022` on
`paramrudra` themselves (in a `screen` session, so it survived the VPN
disconnect that killed the first, non-detached attempt). The listener's
binding was independently confirmed (`ss -tlnp` showing
`LISTEN 0.0.0.0:8022`, a local `curl` returning `200`) before drawing any
conclusion from the external test result, ruling out "the listener never
started" as an alternative explanation for a timeout.

## Evidence gathered

All tests below were run from the operator's Mac with the university VPN
(`vpn.iiitd.edu.in`) disconnected — public IP `103.214.60.35`, distinct
from the VPN's own exit IP `103.25.231.2`, confirming a genuine external
vantage point.

| Target | Port | Result | Interpretation |
|---|---|---|---|
| `192.168.22.23` (`a6000`) | 22 (SSH) | `nc -zv`: **timed out** | Private address, unroutable from the public internet — expected, and confirmed. |
| `paramrudra.iuac.res.in` | 4422 (SSH) | `nc -zv` / real `ssh`: **succeeded immediately** | SSH is open to the general internet on this one specific port. |
| `paramrudra.iuac.res.in` | 8022 (test listener, confirmed bound: `ss -tlnp` shows `LISTEN 0.0.0.0:8022`; local `curl` → `200`) | `nc -zv` / `curl`: **timed out** (not refused — silently dropped) | The institutional firewall allows inbound SSH specifically and drops everything else; this is not an OS-level rejection (which would show as "connection refused"), it's a network-level silent drop consistent with a stateful firewall allow-listing one port. |

**Conclusion: neither candidate environment allows a coordinator to dial
into a node on an arbitrary port.** `a6000`/`h100` are unreachable by
address alone; `paramrudra` is unreachable on anything but its one
allow-listed SSH port, which isn't a port Mycelium's own transport could
claim (it's the institution's, for interactive login). Both point to the
same architecture: **the node must dial out to the coordinator and hold
the connection open**, matching the BOINC/ngrok/torrent-tracker pattern
`phase-0-foundation.md` already named as the likely outcome.

**Decision record: new ADR, `docs/adr/0002-node-transport-model.md`.**
This is an architectural decision (which side initiates the connection,
and why) with the same durability and shape as ADR-0001 — worth a
standalone record independent of any one phase doc, per the existing
pattern. `docs/phases/phase-0-foundation.md`'s open-risk bullet becomes a
short resolved note linking to it, the same relationship ADR-0001 already
has with the PRD.

## Explicitly out of scope for this issue

Actually implementing the dial-out transport (the node-side connection-
holding client, the coordinator-side listener that accepts and tracks
those connections) — this issue is the *decision*, not the
implementation; that's a later Phase 0 ticket (transport/routing work).
Testing every candidate node exhaustively — `a6000`/`h100` share an
environment and were treated as one data point; `paramrudra` is the
second. Re-verifying `a6000`'s hardware specs (already covered by
[issue #2](2026-08-14-issue-2-dependency-pinning-design.md)) or install
path (already covered by
[issue #3](2026-08-14-issue-3-developer-setup-guide-design.md)) — this
issue is scoped to network reachability only.
