# 2. Node transport model: dial-out (node-initiated), not dial-in

## Status

Accepted — 2026-08-14.

## Context

Phase 0's coordinator and node agent need to hold a connection over which
requests and responses flow. Two directions are possible: the coordinator
dials into the node (requires the node to be inbound-reachable), or the
node dials out to the coordinator and holds that connection open (the
node needs only outbound access; the coordinator is the side that must be
publicly reachable) — the pattern BOINC, ngrok, and torrent trackers all
use.

[docs/phases/phase-0-foundation.md](../phases/phase-0-foundation.md)
flagged this as an open, untested assumption: the operator's candidate
Phase 0 nodes sit behind a CDAC HPC allocation and a university VPN,
which "likely" (not confirmed) meant no inbound connectivity.

## Investigation

Live reachability tests were run against both classes of real candidate
node, from a genuinely external network vantage point — the operator's
own internet connection, with the university VPN (`vpn.iiitd.edu.in`)
explicitly disconnected for the test. The vantage point was confirmed
external by a change in public IP (from the VPN's own exit IP
`103.25.231.2` to the raw connection's `103.214.60.35`) — without that
check, a "public reachability" test run through a still-connected VPN
would have silently tested from inside the university network instead.

| Target | Port | Result | Interpretation |
|---|---|---|---|
| `192.168.22.23` (`a6000`, IIITD lab GPU machine); `h100` shares the same private-VPN-only network posture at `192.168.3.215` | 22 (SSH) | `nc -zv`: **timed out** | Private RFC1918 address — unroutable from the public internet by definition, and confirmed directly. |
| `paramrudra.iuac.res.in` (`14.139.62.247`) (IUAC/CDAC HPC login node) | 4422 (SSH) | `nc -zv` / real `ssh`: **succeeded immediately** | SSH is open to the general internet on this one specific port. |
| `paramrudra.iuac.res.in` (`14.139.62.247`) | 8022 (test listener — independently confirmed bound: `ss -tlnp` showed `LISTEN 0.0.0.0:8022`; a local `curl` returned `200`) | `nc -zv` / `curl`: **timed out**, not refused | A silent drop, not an OS-level rejection — consistent with a stateful firewall allow-listing only port 4422 and dropping everything else. |

Full narrative of the investigation (including the permission and VPN-
reconnect complications encountered) is in
[the design spec](../superpowers/specs/2026-08-14-issue-4-node-network-reachability-design.md).

## Decision

**The node agent dials out to the coordinator and holds the connection
open. The coordinator never initiates a connection to a node.**

Neither candidate environment allows a coordinator to dial into a node on
an arbitrary port: `a6000`/`h100` are unreachable by address alone (private
IPs, VPN-only), and on `paramrudra`, no port we tested other than 4422
(SSH) answers — that port is the institution's for interactive login, not
a port Mycelium's transport could claim. Both point to the same
architecture, matching the BOINC/ngrok/torrent-tracker pattern
`docs/phases/phase-0-foundation.md` already named as the likely outcome.

## Consequences

- The coordinator is the only side that needs a stable, inbound-reachable
  address — already how `docs/phases/phase-0-foundation.md` describes it
  ("a single, publicly-reachable service"); this ADR makes explicit that
  the node side is designed the opposite way.
- The node agent needs outbound-only network access, plus a reconnect/
  keepalive strategy for its held connection — a Phase 0 implementation
  detail for whichever ticket builds the transport, not decided here.
- Outbound egress from the node environments was assumed, not tested —
  every reachability test run here was inbound (coordinator dialing into
  a node), not the converse. In particular, all `paramrudra` evidence
  covers its login node only; the machine that would actually run vLLM is
  an HPC compute node behind it, and compute nodes commonly have more
  restricted (sometimes proxy-only or no) internet egress than their
  login node. This should be verified before the dial-out transport is
  actually built.
- This holds regardless of which specific nodes join later: the
  underlying reason (institutional firewalls default to deny-inbound;
  lab machines sit behind VPN-only private addressing) is structural, not
  particular to `a6000`/`h100`/`paramrudra` — a safe general assumption
  for Phase 0's whole node pool.
- Consistent with `docs/phases/phase-0-foundation.md`'s user story #5
  ("I want Phase 0's architecture to not paint Phase 1 (public nodes)
  into a corner") — dial-out is exactly the BOINC/ngrok pattern already
  named there as the Phase 1-compatible approach.
- Out of scope here: actually implementing the dial-out transport (the
  node-side connection-holding client, the coordinator-side listener that
  accepts and tracks those connections) — a later Phase 0 ticket.
