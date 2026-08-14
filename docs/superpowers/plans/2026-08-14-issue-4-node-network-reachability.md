# Issue #4 — Node Network Reachability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write down the resolved node-transport decision (node dials out to the coordinator and holds the connection open; the coordinator never dials into a node) as a new ADR, and update `docs/phases/phase-0-foundation.md`'s "Node network reachability is unconfirmed" open risk to reflect it.

**Architecture:** One new ADR (`docs/adr/0002-node-transport-model.md`) carries the full investigation, evidence, and decision — matching the existing `docs/adr/0001-project-name.md` pattern. `docs/phases/phase-0-foundation.md`'s open-risk bullet is replaced with a short resolved note linking to the ADR, the same relationship ADR-0001 already has with the PRD (`Readme.md`).

**Tech Stack:** Markdown docs only. No code. The underlying investigation (live reachability tests against real nodes) is already complete — recorded in `docs/superpowers/specs/2026-08-14-issue-4-node-network-reachability-design.md` — these tasks transcribe that evidence into the permanent decision record. Do not re-run the live network tests (they required disconnecting the operator's VPN and coordinating a listener process on a real remote HPC login node — already done once, deliberately not repeated).

## Global Constraints

- The decision: the node agent dials out to the coordinator and holds the connection open (the BOINC/ngrok/torrent-tracker pattern); the coordinator never initiates a connection to a node.
- Evidence to cite (from `docs/superpowers/specs/2026-08-14-issue-4-node-network-reachability-design.md`'s evidence table — copy these facts verbatim, do not soften or generalize them):
  - `a6000` (`192.168.22.23`) — private RFC1918 address; `nc -zv 192.168.22.23 22` from a genuinely external vantage point (university VPN disconnected, confirmed by public IP changing from the VPN's exit IP `103.25.231.2` to the raw connection's `103.214.60.35`) **timed out**.
  - `paramrudra` (`paramrudra.iuac.res.in`, public IP `14.139.62.247`) — SSH port 4422: `nc -zv` / real `ssh` **succeeded immediately** from the same external vantage point.
  - `paramrudra` — arbitrary test port 8022 (a `python3 -m http.server 8022` instance, independently confirmed bound with `ss -tlnp` showing `LISTEN 0.0.0.0:8022` and a local `curl` returning `200`, ruling out "never started" as an alternative explanation): `nc -zv` / `curl` from the same external vantage point **timed out** — a silent drop, not a "connection refused," consistent with a stateful firewall allow-listing only port 4422.
- New ADR file path: `docs/adr/0002-node-transport-model.md`, following `docs/adr/0001-project-name.md`'s section structure (`# N. Title`, `## Status`, `## Context`, `## Decision`, `## Consequences`).
- `docs/phases/phase-0-foundation.md`'s "Open risks / unresolved decisions" section currently has exactly 3 bullets (node network reachability, node auth mechanism, exact model choice) — only the first (node network reachability) changes; the other two stay untouched, word-for-word.
- Relative link from `docs/phases/phase-0-foundation.md` to the new ADR: `../adr/0002-node-transport-model.md` (same relative-path pattern the file already uses for `../adr/0001-project-name.md`).

---

### Task 1: `docs/adr/0002-node-transport-model.md`

**Files:**
- Create: `docs/adr/0002-node-transport-model.md`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the ADR file at the path above, containing a `## Consequences` section (or equivalent) that Task 2 links to from `docs/phases/phase-0-foundation.md`. Do not rename the file or change its heading structure after this task — Task 2's link target depends on the exact path.

- [ ] **Step 1: Confirm the ADR doesn't exist yet (red)**

Run: `ls docs/adr/0002-node-transport-model.md`
Expected: `ls: docs/adr/0002-node-transport-model.md: No such file or directory` (or platform equivalent).

- [ ] **Step 2: Write `docs/adr/0002-node-transport-model.md` with this exact content**

```markdown
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
| `192.168.22.23` (`a6000`, IIITD lab GPU machine) | 22 (SSH) | `nc -zv`: **timed out** | Private RFC1918 address — unroutable from the public internet by definition, and confirmed directly. |
| `paramrudra.iuac.res.in` (IUAC/CDAC HPC login node) | 4422 (SSH) | `nc -zv` / real `ssh`: **succeeded immediately** | SSH is open to the general internet on this one specific port. |
| `paramrudra.iuac.res.in` | 8022 (test listener — independently confirmed bound: `ss -tlnp` showed `LISTEN 0.0.0.0:8022`; a local `curl` returned `200`) | `nc -zv` / `curl`: **timed out**, not refused | A silent drop, not an OS-level rejection — consistent with a stateful firewall allow-listing only port 4422 and dropping everything else. |

Full narrative of the investigation (including the permission and VPN-
reconnect complications encountered) is in
[the design spec](../superpowers/specs/2026-08-14-issue-4-node-network-reachability-design.md).

## Decision

**The node agent dials out to the coordinator and holds the connection
open. The coordinator never initiates a connection to a node.**

Neither candidate environment allows a coordinator to dial into a node on
an arbitrary port: `a6000`/`h100` are unreachable by address alone (private
IPs, VPN-only), and `paramrudra` is unreachable on anything but its one
allow-listed SSH port — which is the institution's for interactive login,
not a port Mycelium's transport could claim. Both point to the same
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
- This holds regardless of which specific nodes join later: the
  underlying reason (institutional firewalls default to deny-inbound;
  lab machines sit behind VPN-only private addressing) is structural, not
  particular to `a6000`/`h100`/`paramrudra` — a safe general assumption
  for Phase 0's whole node pool.
- Consistent with `docs/phases/phase-0-foundation.md`'s user story #5
  ("Phase 0's architecture should not paint Phase 1 into a corner") —
  dial-out is exactly the BOINC/ngrok pattern already named there as the
  Phase 1-compatible approach.
- Out of scope here: actually implementing the dial-out transport (the
  node-side connection-holding client, the coordinator-side listener that
  accepts and tracks those connections) — a later Phase 0 ticket.
```

- [ ] **Step 3: Verify the evidence table matches the design spec (green)**

Run:

```bash
grep -A 5 "192.168.22.23" docs/superpowers/specs/2026-08-14-issue-4-node-network-reachability-design.md
grep -A 5 "8022" docs/superpowers/specs/2026-08-14-issue-4-node-network-reachability-design.md
```

Expected: both greps return lines confirming the same facts just written
into the ADR (timeout for `192.168.22.23`, timeout for port 8022 despite
a confirmed-bound listener, immediate success for port 4422) — this
checks the ADR is a faithful transcription of the already-gathered
evidence, not a re-run of the live test.

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0002-node-transport-model.md
git commit -m "docs: add ADR-0002, node transport model (dial-out)"
```

---

### Task 2: Update `docs/phases/phase-0-foundation.md`'s open risk

**Files:**
- Modify: `docs/phases/phase-0-foundation.md` (the "Related:" line near the top, and the first bullet of "## Open risks / unresolved decisions")

**Interfaces:**
- Consumes: `docs/adr/0002-node-transport-model.md` from Task 1 (links to it; do not modify Task 1's file).
- Produces: nothing further consumed by other tasks — this is the last task in the plan.

- [ ] **Step 1: Confirm the risk still reads as unresolved (red)**

Run: `grep -n "Node network reachability is unconfirmed" docs/phases/phase-0-foundation.md`
Expected: one matching line — the bullet has not yet been updated.

- [ ] **Step 2: Add the ADR-0002 link to the "Related:" line**

Find this line near the top of `docs/phases/phase-0-foundation.md`:

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md)
```

Replace it with (inserting the new ADR link right after ADR-0001's):

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md)
```

- [ ] **Step 3: Replace the "Node network reachability" open-risk bullet**

Find this bullet in the "## Open risks / unresolved decisions" section:

```markdown
- **Node network reachability is unconfirmed.** The operator's candidate machines sit behind a CDAC HPC environment and a university VPN, which likely means no inbound connectivity to them. If so, the coordinator cannot dial into a node — the node would need to dial out and hold a connection open instead (the approach BOINC/ngrok/torrent trackers use). This is **not yet designed for**; it needs to be confirmed against the real hardware before the coordinator↔node transport is finalized.
```

Replace it with:

```markdown
- **Node network reachability: resolved.** Confirmed against real candidate nodes — neither the VPN-gated lab machines (`a6000`/`h100`, private addresses, unreachable from the public internet by definition) nor the CDAC/IUAC HPC login node (`paramrudra`, a public IP where only its SSH port answers from outside) accept inbound connections on an arbitrary port. The node agent dials out to the coordinator and holds the connection open — the BOINC/ngrok/torrent-tracker pattern this doc already anticipated. See [ADR-0002](../adr/0002-node-transport-model.md) for the full investigation and decision.
```

Do not touch the "Node auth mechanism is a placeholder" or "Exact model choice is pending real hardware specs" bullets that follow it — they stay exactly as they are.

- [ ] **Step 4: Verify the update (green)**

Run:

```bash
grep -n "Node network reachability is unconfirmed" docs/phases/phase-0-foundation.md
```

Expected: no output (the old unresolved wording is gone).

```bash
grep -n "Node network reachability: resolved" docs/phases/phase-0-foundation.md
grep -n "ADR-0002" docs/phases/phase-0-foundation.md
grep -c "Node auth mechanism is a placeholder\|Exact model choice is pending real hardware specs" docs/phases/phase-0-foundation.md
```

Expected: the first two each return one matching line; the third returns
`2` — both untouched bullets are still present, confirming nothing else
in the "Open risks" section was disturbed.

- [ ] **Step 5: Confirm the ADR link resolves**

Run: `ls docs/adr/0002-node-transport-model.md`
Expected: the file exists (created by Task 1) — the new relative link
in `docs/phases/phase-0-foundation.md` (`../adr/0002-node-transport-model.md`,
resolved from `docs/phases/`) points at a real file.

- [ ] **Step 6: Commit**

```bash
git add docs/phases/phase-0-foundation.md
git commit -m "docs: resolve node network reachability risk, link ADR-0002"
```
