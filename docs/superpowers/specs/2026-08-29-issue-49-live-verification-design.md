# Issue #49 — Live-Hardware + Real-GitHub Verification of Phase 1 — Design

Date: 2026-08-29
Status: Approved, not yet executed
Issue: [#49 — Live-hardware + real-GitHub verification of Phase 1's open-network flow](https://github.com/Zenkai-Dynamics/Mycelium/issues/49)
Blocked by: [#48 — Register Mycelium's real GitHub App](https://github.com/Zenkai-Dynamics/Mycelium/issues/48) (closed)
Precedent: [issue #11's design doc, "Live verification" section](2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md)
(issue #25 — the pattern this doc's own eventual results section follows)

This is the condensed record of the decisions made while brainstorming/
grilling issue #49, before running it. Like #38/#48, this ticket has no
code in it — it's a live-execution runbook, not an architecture — so no
separate implementation-plan doc follows this one. Once the session
actually runs, its results get appended to *this* doc as a
`## Live verification` section, matching how issue #25's results were
appended to issue #11's own design doc rather than scattered across
every ticket the session touches.

## What issue #49 asks for

Everything Phase 1 shipped (#33 keypair identity, #39 GitHub verification,
#35 per-identity cap, #36 reputation-weighted selection, #37 manual ban,
#34 device-flow sign-in) is merged and unit/integration-tested against
injected fakes, but none of it has run against a real GitHub identity or
real GPU hardware. #49 proves four things end to end, on the operator's
`a6000` box, using the real GitHub App #48 registered: (1) a real node
completes the device-flow sign-in and registers; (2) the per-identity cap
rejects a real registration once the identity is at its limit; (3)
reputation-weighted selection statistically favors a real reliable node
over a real unreliable one, without a hard cutoff; (4)
`mycelium-coordinator-ban` disconnects a real identity's node(s) and
blocks its future registration.

## Decisions made

**Topology: everything on `a6000`, loopback — not #25's fuller 3-host
separation.** #25 already proved the underlying transport works across
genuinely separate machines (client, coordinator, and nodes on three
different hosts); re-proving that here would retread ground rather than
cover new ground, since #49's actual subject is Phase 1's *trust* layer
(identity, cap, reputation, ban), which is orthogonal to network topology.
Matches #10's original simplest pattern instead: coordinator and both
nodes on `a6000`, client run from the same box or the operator's own
machine.

**Session shape: one continuous coordinator+node session for all four
scenarios, not four separate ones.** Register once, then layer cap →
reputation → ban on top of the same real bound identity. Avoids repeating
vLLM's multi-minute weight-load boot up to four times, and matches how
#25 itself ran multiple scenarios back to back in one live session rather
than restarting between them.

**Two real nodes, not one — because scenario 3 needs a fair comparison.**
Scenario 1 only strictly needs one real node, but scenario 3 (reputation-
weighted selection) requires comparing *selection frequency* between a
reliable and an unreliable node hosting the same model — that comparison
is only meaningful if both are real, running nodes capable of actually
serving the model, not one real node and something synthetic. So Node A
and Node B both run real `vllm serve` instances from the start; Node B
starts as the one that later accumulates real failures.

**Per-identity cap (scenario 2): one lightweight stand-in fills the cap,
a second lightweight stand-in gets rejected — not four full real nodes.**
The per-identity cap check fires when the coordinator receives a signed
`register` message; it has no dependency on whether the sending node's
vLLM is healthy or even exists. Since Node A and Node B (both needed
anyway for scenario 3) already occupy two of the cap's three default
slots under the same real bound identity, only one additional
registration is needed to fill the cap, and one more after that to
observe the rejection — both as lightweight scripts that reuse
`mycelium.crypto`/`mycelium.node.registration`/`mycelium.node.identity`
directly to send a real, validly-signed registration over a real
websocket connection to the real coordinator, without ever starting
vLLM. This is still a fully real exercise of the coordinator's identity-
resolution, cap-enforcement, and network code — a connection that only
ever existed to prove a rejection was never going to serve real traffic
regardless of whether vLLM sat behind it, so skipping that GPU-heavy step
changes nothing about what's actually being verified. Running four full
`mycelium-node` processes (needing up to four simultaneously-idle GPUs)
was considered and rejected as unnecessary GPU time and setup for
identical proof value.

**Correction found during grilling, verified against the actual code:**
`enforce_identity_cap`'s own docstring is explicit — it "counts live
registry occupancy... a disconnected node's old key doesn't count against
the cap," and per #35's own design a node disconnecting frees its slot
*immediately*. A stand-in that registers and then exits would **not**
hold its cap slot. The stand-in script must keep its websocket connection
open (idle — no further traffic needed) for as long as it needs to keep
occupying a slot: from filling the cap in scenario 2, through the second
stand-in's rejection, **and through scenario 4's ban** (see below) — only
closing once the whole session's identity-scoped work is done.

**Reputation (scenario 3): real crash and disconnect signals, not a
forced timeout.** Verified against the actual code (`router.py`,
`request_handler.py`, `vllm_process.py`), not assumed:

- *Crash* (`NodeError`/`record_crash`): killing only Node B's `vllm
  serve` subprocess — leaving its `mycelium-node` process and coordinator
  websocket connection alive — makes the next completion request fail
  fast and genuinely: `urllib`'s call to the now-dead local vLLM port
  gets connection-refused almost immediately, `request_handler.py`'s
  broad `except Exception` turns that into a real `complete_error`, and
  the coordinator's `router.route_request` raises `NodeError`. Fast,
  repeatable, no artificial code paths involved. Concrete, precedented
  command (matches `docs/OPERATIONS.md`'s own troubleshooting section —
  not a new technique): `pgrep -f "vllm serve.*--port <B's port>"` to
  find the PID, `ps -o pgid= -p <pid>` to confirm its process-group id,
  then `kill -9 -<pgid>` (the leading `-` targets the whole group,
  leaving Node B's own `mycelium-node` process, a separate process group,
  untouched).
- *Disconnect* (`NodeDisconnectedError`/`record_disconnect`): `kill -9`
  on Node B's whole `mycelium-node` process, exactly as #25 already
  proved live for Phase 0's failover. Node B then reconnects (cached
  token, same identity) so it's eligible for selection again with its
  accumulated bad counters intact — counters live in the coordinator's
  in-memory registry, keyed by public key, and persist across a node's
  own reconnects for as long as the coordinator itself keeps running.
- **Concrete numbers, decided in advance rather than eyeballed live.**
  Reputation weight is `(completions + 1) / (total + 1)`, Laplace-smoothed
  (confirmed from `registry.py`, not assumed). Inject exactly 3 crashes +
  1 disconnect on Node B (4 total pre-batch failures, weight ≈ 0.2) while
  Node A stays clean (weight = 1.0) — an initial ≈5:1 selection-weight
  ratio. A batch of 20 client requests follows: expected result is Node A
  picked meaningfully more than half the time (the initial math suggests
  roughly 80%+, trending higher as any further Node B picks during the
  batch itself push its weight down further) **and** Node B picked at
  least once, proving the "never a hard cutoff" property. Having this
  computed in advance gives the operator something concrete to check the
  actual result against, not just an impression of "A seems to win more."
- *Timeout* (`NodeTimeoutError`/`record_timeout`) is a **documented,
  accepted gap for this pass**, matching #25's own precedent of accepting
  the h100 permission issue as a real, separate gap rather than forcing
  an artificial reproduction. The code is deliberately built so this
  case almost never fires organically: `vllm_process.COMPLETE_TIMEOUT_SECONDS`
  (120s) is set 10s *under* `router.NODE_COMPLETE_TIMEOUT_SECONDS` (130s)
  specifically so a slow node's own timeout fires first and produces a
  `complete_error` (→ `NodeError`/crash), not a coordinator-side timeout
  — confirmed via `router.py`'s own comment on why the two constants are
  10s apart. A genuine `NodeTimeoutError` needs a node to go silent on
  one specific reply while continuing to answer the websocket's ping/pong
  (otherwise the 40s heartbeat window — `PING_INTERVAL_SECONDS` +
  `PING_TIMEOUT_SECONDS`, both 20s — trips first and produces a disconnect
  instead). Attempting to force this (e.g. `SIGSTOP` timed precisely
  between the heartbeat and completion windows) was considered and
  rejected: finicky, likely to just produce another disconnect, and for a
  case the design already treats as a narrow edge rather than a normal
  failure mode.

**Recorded-results scope: incidental `mycelium-coordinator-status`
coverage, not a separate effort.** Issues #33 and #36 added identity-
fingerprint and reputation-counter fields to `mycelium-coordinator-status`'s
output, but neither ticket's own live-hardware verification ever ran (no
live-verification ticket existed for either at the time). Since this
session already runs `mycelium-coordinator-status` repeatedly to observe
cap/reputation/ban results, capturing that its output correctly displays
those fields under real conditions costs nothing beyond noting it in the
transcript — closing a related, real gap opportunistically rather than
either ignoring it or scoping a whole separate effort around it.

## Session runbook

1. **Prereqs.** Confirm SSH reachability to `a6000`. Run `nvidia-smi` —
   confirm at least 2 GPUs are idle enough to not degrade another user's
   job (matching #25's own caution; if fewer than 2 are free, wait or
   pick a different pair rather than proceeding).
2. **Setup.** Fresh token + self-signed cert generated on `a6000`;
   `mycelium-coordinator` started there.
3. **Scenario 1.** Node A starts with no `--github-token-file` and no
   cached `~/.mycelium/github-token` — completes the real device-flow
   sign-in (code printed, authorized in a browser), registers. Confirmed
   via `mycelium-coordinator-status`.
4. **Node B** starts on a second GPU, same model, pointed at the same
   cached token path Node A just populated (same real identity — the
   legitimate multi-GPU-volunteer case the docs already describe).
   Confirmed registered.
5. **Baseline.** A handful of client requests confirm both nodes are
   genuinely reachable and round-robin-eligible before any failure is
   injected — establishes the "before" state, matching #25's own style.
6. **Scenario 2.** One lightweight stand-in registration (direct use of
   `mycelium.crypto`/`identity`/`registration`, no vLLM) under the same
   identity, using its own fresh keypair and the same cached token —
   succeeds, filling the cap at 3. **It stays connected (idle) from here
   through scenario 4** — its cap slot is only held while its websocket
   is open, per `enforce_identity_cap`'s own documented behavior. A
   second stand-in attempt is rejected; the exact `registration_rejected`
   reason is recorded.
7. **Scenario 3.** Node B's `vllm serve` subprocess is killed via
   `pgrep`/`ps -o pgid=`/`kill -9 -<pgid>` (its `mycelium-node` process
   stays up). 3 client requests routed to Node B are recorded failing
   with a real `NodeError`; `mycelium-coordinator-status` shows Node B's
   crash counter at 3. Node B's whole process is then `kill -9`'d for one
   real disconnect (counter → 1), and restarted (reconnects with its
   cached token, no re-auth) — 4 total pre-batch failures, weight ≈ 0.2
   against Node A's clean weight of 1.0. A batch of 20 client requests is
   sent; the A/B split is recorded against the ≈80%+/at-least-once
   expectation computed in the design decisions above.
8. **Scenario 4.** `mycelium-coordinator-ban --identity <login>` is run
   against the real bound identity. All three currently-connected
   entities under it — Node A, Node B, and the still-open scenario-2
   stand-in — are confirmed disconnected immediately
   (`disconnected_count: 3` in the ban response,
   `mycelium-coordinator-status` shows none registered). A further
   registration attempt from that identity (Node A restarting, or a
   fresh stand-in) is confirmed rejected with the banned-identity reason.
9. **Recording.** A `## Live verification` section, dated to when the
   session actually runs, is appended to *this* design doc — real command
   transcripts, real output, any real findings/bugs encountered along the
   way (matching #25's own style, including its precedent of recording a
   real bug found mid-session rather than silently working around it). If
   a genuine bug turns up, it becomes its own follow-up ticket per #49's
   own acceptance criteria — not silently patched as part of this
   ticket's commits, since this ticket makes no code changes.

## Explicitly out of scope for this issue

A genuine coordinator-side `NodeTimeoutError` reproduction (documented gap
above). Full 3-host network separation (already proven in #25). Any code
change — a real bug found during the session becomes its own ticket. Any
change to #33/#36's status-display code — this session only *observes and
records* that it already works, it doesn't add new coverage to it.
