# Issue #9 — Node Heartbeat & Liveness Tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove and document that the coordinator already drops a node from its healthy registry when that node goes silent — whether cleanly killed or unresponsive due to a network partition/frozen process — within a bounded, documented time window, using the WebSocket ping/pong keepalive #5/#8 already built, with no new heartbeat protocol.

**Architecture:** No new modules, no new message types, no changed default timeouts. `coordinator/server.py`'s `serve()` already configures `websockets.serve(..., ping_interval=PING_INTERVAL_SECONDS, ping_timeout=PING_TIMEOUT_SECONDS)` (both `20`), and `_handle_registration`'s `finally: registry.unregister(node_id, websocket)` already fires whenever the connection closes for any reason, including a `websockets`-library-initiated close after a missed pong. This plan adds one regression test proving that specific path (silent node → ping timeout → registry drop) — previously only the clean-disconnect and abrupt-transport-close paths were tested — plus documentation of the derived ~40s worst-case bound in the code and in `docs/phases/phase-0-foundation.md`.

**Tech Stack:** Python 3.11+, `pytest`/`pytest-asyncio` (already pinned), `websockets` (already pinned). No new dependencies.

## Global Constraints

- No new application-level heartbeat message. `PING_INTERVAL_SECONDS = 20` / `PING_TIMEOUT_SECONDS = 20` in both `coordinator/server.py` and `node/connection.py` (already equal, already enforced by `test_server_and_connection_agree_on_keepalive_settings` in `tests/test_integration.py`) **are** the heartbeat mechanism. Do not add a `{"type": "heartbeat"}` message or change these two values.
- Derived worst-case detection bound: `PING_INTERVAL_SECONDS + PING_TIMEOUT_SECONDS` ≈ 40s from "node goes silent" to "dropped from the registry." This is the answer to the "bounded, documented time window" acceptance criterion — document it, don't retune it.
- Scope is coordinator-tracks-node liveness only. Node-side detection of a dead coordinator is unchanged, already built by #5, and out of scope here.
- Verification is simulated-test-only — no new real-hardware run for this issue (unlike #4/#6/#7/#8). The clean-process-kill case is already live-verified by #8; what's new here is the silent/no-close-frame case.
- New test technique: reuse the zombie-connection simulation already established in `tests/coordinator/test_server.py`'s `test_duplicate_node_id_registration_acks_promptly_even_if_old_connection_is_unresponsive` — a real client `websocket`'s `.transport.pause_reading()` stops it from processing any incoming bytes (including ping frames), so it can never send a pong, without ever sending a close frame either. Combine with `monkeypatch.setattr(server, "PING_INTERVAL_SECONDS", ...)` / `"PING_TIMEOUT_SECONDS"` set to small values for test speed, matching the existing `monkeypatch.setattr(server, "FIRST_MESSAGE_TIMEOUT_SECONDS", 0.3)` pattern in `test_connection_with_no_message_is_closed_after_timeout`.
- **TDD deviation, stated explicitly so it isn't mistaken for a mistake mid-execution:** Task 1's test is expected to **pass immediately**, with no production code change. This is not "testing after" in the sense TDD warns against — no new behavior is being added; the test locks in and proves a specific code path (ping-timeout-triggered close → `registry.unregister`) that #5/#8 already built but no existing test exercised (existing tests only cover a clean `.close()` and an abrupt `.transport.close()`, neither of which goes through the ping/pong timeout at all). Do not write new production code to make this test pass — if it fails, that's a real bug in already-shipped #5/#8 behavior, not a sign this task needs implementation code; stop and use `superpowers:systematic-debugging` instead of patching the test to fit.

---

### Task 1: Regression test — silently unresponsive node is dropped within the ping-timeout window

**Files:**
- Modify: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `server.serve`, `server.PING_INTERVAL_SECONDS`, `server.PING_TIMEOUT_SECONDS` (all already exist, from #5/#8) — no new interfaces from other tasks.
- Produces: nothing new consumed elsewhere — this task adds a test only, no production code.

- [ ] **Step 1: Write the test**

Add to `tests/coordinator/test_server.py`, immediately after `test_duplicate_node_id_registration_acks_promptly_even_if_old_connection_is_unresponsive`:

```python
async def test_silently_unresponsive_node_is_dropped_within_ping_timeout_window(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PING_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(server, "PING_TIMEOUT_SECONDS", 0.2)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await node_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
        ))
        await node_ws.recv()  # consume the "registered" ack

        # Simulate the node going silent (network partition, frozen process):
        # stop processing incoming bytes, so it can never answer a ping with
        # a pong — without ever sending a WebSocket close frame either. This
        # is a different failure mode than test_disconnected_node_is_removed_
        # from_registry (clean close) or test_server_survives_abnormal_
        # disconnect (abrupt transport close) — neither of those goes through
        # the ping/pong timeout path this test targets.
        node_ws.transport.pause_reading()

        # Worst case per the design doc for issue #9: PING_INTERVAL_SECONDS
        # to notice the silence, plus PING_TIMEOUT_SECONDS for the pong that
        # never arrives.
        await asyncio.sleep(server.PING_INTERVAL_SECONDS + server.PING_TIMEOUT_SECONDS + 0.3)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/coordinator/test_server.py::test_silently_unresponsive_node_is_dropped_within_ping_timeout_window -v`

Expected: **PASS**, not fail — see the "TDD deviation" note in Global Constraints above. This proves `PING_INTERVAL_SECONDS`/`PING_TIMEOUT_SECONDS` plus the existing `registry.unregister` cleanup in `_handle_registration`'s `finally` block already handle the silent-node case correctly, with zero production code changes needed.

If it fails or hangs instead: do not try to make it pass by adding production code. Stop and invoke `superpowers:systematic-debugging` — a failure here means the ping/pong mechanism or the registry cleanup path doesn't behave the way #5/#8's design assumed, which is a real bug worth understanding before touching anything.

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: PASS, everything green, no change to any other test's outcome.

- [ ] **Step 4: Commit**

```bash
git add tests/coordinator/test_server.py
git commit -m "test: prove silently unresponsive nodes are dropped within the ping-timeout window"
```

---

### Task 2: Document the heartbeat/liveness mechanism

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Modify: `src/mycelium/node/connection.py`
- Modify: `src/mycelium/node/registration.py`
- Modify: `docs/phases/phase-0-foundation.md`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: nothing consumed elsewhere — comments and docstrings only, no behavior or signature changes.

- [ ] **Step 1: Update `server.py`'s module docstring and add a comment above the ping constants**

In `src/mycelium/coordinator/server.py`, replace the module docstring:

```python
"""WebSocket server the coordinator runs to accept dial-out node connections.

See ADR-0002 for why nodes dial out rather than the coordinator dialing in.
Handles the registration handshake and registry status queries — see the
design doc for issue #8. Heartbeat/liveness tracking beyond registration
(#9) and routing a client request (#10) are not this module's job yet.
"""
```

with:

```python
"""WebSocket server the coordinator runs to accept dial-out node connections.

See ADR-0002 for why nodes dial out rather than the coordinator dialing in.
Handles the registration handshake and registry status queries — see the
design doc for issue #8. Node liveness is tracked via the WebSocket
ping/pong keepalive (PING_INTERVAL_SECONDS/PING_TIMEOUT_SECONDS below) —
see the design doc for issue #9: a node that goes silent gets its
connection closed by the `websockets` library itself, which
`_handle_registration`'s `finally: registry.unregister(...)` already turns
into a registry drop, the same as any other disconnect. Routing a client
request (#10) is not this module's job yet.
"""
```

Then replace:

```python
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20
FIRST_MESSAGE_TIMEOUT_SECONDS = 10.0
```

with:

```python
# These two also double as #9's node-liveness mechanism: a silent node (no
# pong within PING_TIMEOUT_SECONDS of a ping) has its connection closed by
# the websockets library, which _handle_registration's cleanup already
# turns into a registry drop — see test_silently_unresponsive_node_is_
# dropped_within_ping_timeout_window in tests/coordinator/test_server.py.
# Worst case from "node goes silent" to "dropped from the registry":
# PING_INTERVAL_SECONDS + PING_TIMEOUT_SECONDS ≈ 40s.
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20
FIRST_MESSAGE_TIMEOUT_SECONDS = 10.0
```

- [ ] **Step 2: Add a short comment in `connection.py`**

In `src/mycelium/node/connection.py`, replace:

```python
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20
```

with:

```python
# coordinator/server.py uses these same two values for issue #9's
# node-liveness detection — test_server_and_connection_agree_on_keepalive_
# settings (tests/test_integration.py) keeps them from drifting apart.
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20
```

- [ ] **Step 3: Update `registration.py`'s stale "future heartbeat" reference**

In `src/mycelium/node/registration.py`, replace:

```python
"""Sends the node's registration handshake to the coordinator and awaits
the result.

See the design doc for issue #8. This module owns exactly one exchange:
send {"type": "register", ...}, wait for {"type": "registered"} or
{"type": "registration_rejected", ...}, bounded by a timeout. Everything
after that — holding the connection open, future heartbeat/routing
messages — is connection.py's/cli.py's job, not this module's.
"""
```

with:

```python
"""Sends the node's registration handshake to the coordinator and awaits
the result.

See the design doc for issue #8. This module owns exactly one exchange:
send {"type": "register", ...}, wait for {"type": "registered"} or
{"type": "registration_rejected", ...}, bounded by a timeout. Everything
after that — holding the connection open, future routing messages — is
connection.py's/cli.py's job, not this module's. Liveness tracking (#9)
needs no message from this module at all: it rides on the same
WebSocket's ping/pong keepalive, handled below connection.py, not here.
"""
```

- [ ] **Step 4: Update `docs/phases/phase-0-foundation.md`**

Replace the `Related:` line:

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md)
```

with:

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md) · [Node heartbeat & liveness tracking](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md)
```

Add a new bullet to the end of the `## Open risks / unresolved decisions` section, after the `vLLM must be started with...` bullet:

```markdown
- **Node heartbeat/liveness: resolved.** The coordinator tracks node liveness using the WebSocket ping/pong keepalive already established by #5/#8 (`PING_INTERVAL_SECONDS`/`PING_TIMEOUT_SECONDS`, both 20s) rather than a separate application-level heartbeat message — a node that goes silent, whether cleanly killed or unresponsive due to a network partition or frozen process, is dropped from the registry within ~40s worst case (`PING_INTERVAL_SECONDS + PING_TIMEOUT_SECONDS`). The clean-kill case was already live-verified by #8; #9 adds a regression test for the silent-disconnect case specifically. See [the design doc](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md) for the full decision record.
```

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS, everything green — this task changes only comments/docstrings/docs, no behavior.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/coordinator/server.py src/mycelium/node/connection.py src/mycelium/node/registration.py docs/phases/phase-0-foundation.md
git commit -m "docs: document the ping/pong heartbeat mechanism for issue #9"
```
