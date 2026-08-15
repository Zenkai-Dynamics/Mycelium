# Issue #9 — Node Heartbeat & Liveness Tracking — Design

Date: 2026-08-15
Status: Approved, not yet implemented
Issue: [#9 — Node heartbeat & liveness tracking](https://github.com/Zenkai-Dynamics/Mycelium/issues/9)

This is the condensed record of the decisions made while brainstorming/
grilling issue #9, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established
in the [issue #1](2026-08-12-issue-1-project-skeleton-design.md) through
[issue #8](2026-08-15-issue-8-node-registration-handshake-design.md)
design docs.

## What issue #9 asks for

A registered node sends periodic heartbeats to the coordinator; the
coordinator tracks liveness and automatically drops a node from the
healthy registry if its heartbeats stop. Acceptance criteria: a
registered node sends heartbeats on a fixed interval; the coordinator's
healthy-node list reflects only nodes with a recent heartbeat; killing a
node process causes it to be dropped from the healthy list within a
bounded, documented time window.

## The key finding: this is mostly already built

`connection.py` (#5) and `server.py` (#8) already configure the
WebSocket connection with `ping_interval=20s` / `ping_timeout=20s` on
**both** sides. The `websockets` library sends protocol-level ping
frames automatically at that interval and closes the connection if a
pong doesn't come back within the timeout — no application code
involved. `server.py`'s `_handle_registration` already has a `finally:
registry.unregister(node_id, websocket)` that fires on
`ConnectionClosed`, dropping the node from the registry. Issue #8's live
verification already proved the disconnect side of this end to end: a
real `SIGTERM` to a node process on `a6000` led to the coordinator's
status query showing `No nodes registered.` immediately after.

So the question this design had to resolve wasn't "how do we build
heartbeats" but "do we need a *second*, application-level heartbeat
mechanism on top of the one that already exists."

## Decisions made

**No new application-level heartbeat message.** The transport-level
ping/pong *is* the heartbeat. Rejected adding a `{"type": "heartbeat"}`
message sent by the node on its own timer — it would duplicate a
liveness signal the WebSocket protocol already provides on both sides
for free, adding protocol surface and a second failure-detection path to
keep in sync with the first, for no behavioral gain. The acceptance
criterion "node sends heartbeats to the coordinator on a fixed interval"
is satisfied by the node's own `websockets.connect(...,
ping_interval=PING_INTERVAL_SECONDS)` call, which already sends ping
frames to the coordinator on a fixed interval today.

**Detection window: keep `PING_INTERVAL_SECONDS = 20` /
`PING_TIMEOUT_SECONDS = 20` unchanged, document the derived ~40s
worst-case bound.** These values were already chosen and live-verified
by #5/#8; retuning them is a separate concern from #9's job (proving and
documenting liveness tracking, not re-optimizing its latency). Worst
case: a ping is sent right as a node goes silent (up to
`PING_INTERVAL_SECONDS` late to notice) plus the full
`PING_TIMEOUT_SECONDS` wait for a pong that never arrives — so ~40s from
"node goes silent" to "dropped from the registry." That figure is this
design's answer to the acceptance criterion's "bounded, documented time
window."

**Scope: coordinator tracking node liveness only, not the reverse.**
Issue #9's text and acceptance criteria are entirely about the
coordinator's registry reflecting node health. The node already detects
a dead/silent coordinator via the exact same ping/pong mechanism on its
side and reconnects via #5's tested backoff loop
(`test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop` in
`tests/test_integration.py`) — that path is unchanged, already built,
and out of scope for #9's work.

**Verification: simulated tests only, no new real-hardware run.** Prior
Phase 0 issues (#4, #6, #7, #8) verified against real hardware where the
question under test was genuinely uncertain without it — real network
paths (#4), real GPU/vLLM behavior (#6, #7), a real registration
round-trip (#8). Ping/pong liveness detection is a `websockets` library
guarantee, not Mycelium-specific behavior; #8 already live-verified the
"node process dies → coordinator notices → registry updates" path for a
clean kill. What #9 adds is proving the *silent* case (a connection that
never sends a close frame — network partition, frozen process, unplugged
cable) is handled the same way, which is adequately provable with a
local simulated test.

**New test reuses an existing technique.**
`tests/coordinator/test_server.py` already has a proven way to simulate
an unresponsive peer:
`test_duplicate_node_id_registration_acks_promptly_even_if_old_connection_is_unresponsive`
calls `old_ws.transport.pause_reading()` to make a real client stop
processing incoming frames (including pings) without closing the
connection. #9's new test — in the same file, immediately after that
one — registers a node, pauses its transport's reading the same way,
monkeypatches `PING_INTERVAL_SECONDS`/`PING_TIMEOUT_SECONDS` down to
sub-second values (matching the existing
`test_connection_with_no_message_is_closed_after_timeout` pattern for
`FIRST_MESSAGE_TIMEOUT_SECONDS`) so the test doesn't take 40 real
seconds, and asserts a subsequent status query no longer lists the node
once the timeout has elapsed. No new test infrastructure.

**Code gets a short comment, not a new constant.** A comment above
`PING_INTERVAL_SECONDS`/`PING_TIMEOUT_SECONDS` in `server.py` will note
that these values now double as #9's heartbeat/liveness mechanism and
state the derived ~40s worst-case bound — matching this file's existing
habit of explaining non-obvious reasoning inline (e.g. the
`_close_in_background` comment). No new named constant: the two existing
constants are already the complete, correct source of truth, and a
third derived constant would just be something to keep in sync with
them for no behavioral purpose.

**Docstring cleanup.** `server.py`, `connection.py`, and
`registration.py` each currently have a comment or docstring line
pointing at "#9" as future/out-of-scope work (e.g. server.py's module
docstring: "Heartbeat/liveness tracking beyond registration (#9)... are
not this module's job yet"). These get updated to reflect that #9 is
now done, since #9 is exactly the change that makes those lines stale.

## What actually changes

- `src/mycelium/coordinator/server.py` — a comment above the ping
  constants; docstring update.
- `src/mycelium/node/connection.py` — docstring update only (no ping/pong
  behavior change; documents that these constants now serve #9 too).
- `src/mycelium/node/registration.py` — docstring update only.
- `tests/coordinator/test_server.py` — one new test.
- `docs/phases/phase-0-foundation.md` — note the resolved mechanism and
  the ~40s bound.

No new modules, no new message types, no changed default timeouts, no
new CLI flags.

## Explicitly out of scope for this issue

A distinct application-level heartbeat message or payload (e.g. carrying
node health/load data) — nothing today needs that data, and it can be
added later without disturbing this design if it does. Retuning
`PING_INTERVAL_SECONDS`/`PING_TIMEOUT_SECONDS` for a faster or slower
detection window. Node-side detection of a dead coordinator (already
built by #5, unchanged here). A "degraded/suspect" intermediate registry
state between "registered" and "gone" — the registry stays binary
(present or absent), matching #8's existing model. Real-hardware
verification of the silent-disconnect path. Routing a client request to
a registered node (#10).
