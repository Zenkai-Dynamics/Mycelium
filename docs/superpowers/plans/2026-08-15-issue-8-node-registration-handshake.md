# Issue #8 — Node Registration Handshake — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A node agent registers itself with the coordinator on startup (shared token + model + node ID), the coordinator validates the token and rejects invalid/missing ones with a reason, and a registered node appears in a coordinator-side registry an operator can query with a new `mycelium-coordinator-status` command.

**Architecture:** Two new small modules carry the protocol: `mycelium/coordinator/registry.py` (an in-memory `NodeRegistry` — token check, register/replace, unregister, list) and `mycelium/node/registration.py` (sends the registration message, awaits the coordinator's ack/rejection with a timeout). `coordinator/server.py`'s `_handle_node` reads a node's first message and dispatches on its `"type"` — `"register"` validates against the registry and either rejects-and-closes or registers-and-holds-open; `"status_query"` answers with the current registry and closes. `node/cli.py` calls `registration.register()` right after each fresh connection, before entering its existing wait-until-closed loop; a rejected/timed-out registration falls back into the existing reconnect loop rather than crashing. A new script, `mycelium-coordinator-status`, queries the registry the same way a node would (same cert, same token) but as a one-shot request.

**Tech Stack:** Python 3.11+, stdlib only (`json`, `asyncio`, `socket`, `hmac`, `dataclasses`) plus the `websockets` dependency already pinned. No new dependencies.

## Global Constraints

- Token model: **one shared token** for every node, delivered via `--token-file` on both `mycelium-node` and `mycelium-coordinator` (never a bare `--token` flag, never an environment variable) — the file contains exactly one token string.
- Protocol messages (JSON, sent as WebSocket text frames), exact shapes:
  - Node → coordinator: `{"type": "register", "token": "...", "model": "...", "node_id": "..."}`
  - Coordinator → node, success: `{"type": "registered"}`
  - Coordinator → node, failure: `{"type": "registration_rejected", "reason": "..."}`, then the coordinator closes the connection
  - Operator → coordinator: `{"type": "status_query", "token": "..."}`
  - Coordinator → operator: `{"type": "status", "nodes": [{"node_id": "...", "model": "..."}, ...]}`, then the coordinator closes the connection
- Timeouts, both directions, named `REGISTRATION_TIMEOUT_SECONDS` (node side, in `registration.py`) and `FIRST_MESSAGE_TIMEOUT_SECONDS` (coordinator side, in `server.py`) — both `10.0`, and a test asserts they stay equal (mirrors the existing `PING_INTERVAL_SECONDS`/`PING_TIMEOUT_SECONDS` symmetry pattern between `connection.py`/`server.py`). Node: gives up waiting for the coordinator's response and falls back to the existing reconnect-backoff loop. Coordinator: closes a connection that never sends a first message in time.
- Duplicate `node_id`: the coordinator **replaces** the old registry entry and **actively closes** the old, now-superseded connection.
- Disconnect (any reason): the registry entry is **removed immediately** — no retained/"disconnected" history.
- Status query authenticates with the **same shared token** nodes use — no separate operator credential.
- Token comparison uses `hmac.compare_digest`, not `==` — cheap, stdlib, avoids a timing side-channel for a secret comparison.
- `mycelium-coordinator-status` is a **new script** (new `[project.scripts]` entry, new module), not a subcommand of `mycelium-coordinator` — the existing `mycelium-coordinator --host ...` invocation shape (already shipped, already documented in `docs/SETUP.md`) does not change.
- Tests bind to port `0` and read back the OS-assigned port (`server.sockets[0].getsockname()[1]`) rather than hardcoding a port literal — avoids the port-collision risk flagged in issue #7's final review.
- Match existing module style: `from __future__ import annotations`, a module docstring explaining scope, `SCREAMING_SNAKE_CASE` constants, tests using real local servers/connections rather than mocks (this codebase's established convention throughout `tests/coordinator/` and `tests/node/`).

---

### Task 1: Coordinator registry (`mycelium/coordinator/registry.py`)

**Files:**
- Create: `src/mycelium/coordinator/registry.py`
- Test: `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: nothing from other tasks — first task.
- Produces: `class NodeRegistry(token: str)` with `.check_token(token: str) -> bool`, `.register(node_id: str, model: str, websocket) -> Node | None` (returns the superseded `Node` if `node_id` was already registered, else `None`), `.unregister(node_id: str, websocket) -> None` (no-op unless `websocket` is still the current connection for `node_id`), `.list_nodes() -> list[dict]`. `@dataclass class Node` with `.node_id`, `.model`, `.websocket`. Consumed by Task 3 (`server.py`).

- [ ] **Step 1: Write the failing tests**

Create `tests/coordinator/test_registry.py`:

```python
"""Tests for mycelium.coordinator.registry."""

from mycelium.coordinator.registry import NodeRegistry


def test_check_token_accepts_matching_token():
    registry = NodeRegistry("secret")
    assert registry.check_token("secret") is True


def test_check_token_rejects_wrong_token():
    registry = NodeRegistry("secret")
    assert registry.check_token("wrong") is False


def test_check_token_rejects_missing_token():
    registry = NodeRegistry("secret")
    assert registry.check_token("") is False


def test_register_adds_node_to_list():
    registry = NodeRegistry("secret")
    registry.register("node-a", "Qwen/Qwen2.5-7B-Instruct", websocket="ws-a")
    assert registry.list_nodes() == [{"node_id": "node-a", "model": "Qwen/Qwen2.5-7B-Instruct"}]


def test_register_returns_none_when_no_prior_entry():
    registry = NodeRegistry("secret")
    superseded = registry.register("node-a", "model-a", websocket="ws-a")
    assert superseded is None


def test_register_replacing_existing_node_returns_superseded_entry():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-old")
    superseded = registry.register("node-a", "model-b", websocket="ws-new")
    assert superseded is not None
    assert superseded.websocket == "ws-old"
    assert registry.list_nodes() == [{"node_id": "node-a", "model": "model-b"}]


def test_unregister_removes_matching_connection():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    registry.unregister("node-a", websocket="ws-a")
    assert registry.list_nodes() == []


def test_unregister_does_not_remove_a_newer_replacement():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-old")
    registry.register("node-a", "model-b", websocket="ws-new")
    # The old connection's handler notices it's closed and tries to clean up
    # its own (now-superseded) entry — must not delete the newer one.
    registry.unregister("node-a", websocket="ws-old")
    assert registry.list_nodes() == [{"node_id": "node-a", "model": "model-b"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.coordinator.registry'`

- [ ] **Step 3: Write the implementation**

Create `src/mycelium/coordinator/registry.py`:

```python
"""In-memory registry of currently-registered nodes.

See the design doc for issue #8. Tracks which nodes are registered, the
model each hosts, and the live connection to reach them. Mutated only
from the coordinator's single asyncio event loop — no locking needed,
since plain dict operations don't yield control mid-mutation.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any


@dataclass
class Node:
    node_id: str
    model: str
    websocket: Any


class NodeRegistry:
    """Holds the shared token and the current set of registered nodes."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._nodes: dict[str, Node] = {}

    def check_token(self, token: str) -> bool:
        return hmac.compare_digest(token, self._token)

    def register(self, node_id: str, model: str, websocket: Any) -> Node | None:
        """Add or replace node_id's entry. Returns the superseded Node if
        one existed under this node_id, else None — the caller is
        responsible for closing the superseded connection."""
        previous = self._nodes.get(node_id)
        self._nodes[node_id] = Node(node_id=node_id, model=model, websocket=websocket)
        return previous

    def unregister(self, node_id: str, websocket: Any) -> None:
        """Remove node_id's entry, but only if it's still this exact
        connection — a newer registration may have already replaced it."""
        current = self._nodes.get(node_id)
        if current is not None and current.websocket is websocket:
            del self._nodes[node_id]

    def list_nodes(self) -> list[dict]:
        return [{"node_id": n.node_id, "model": n.model} for n in self._nodes.values()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_registry.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: in-memory node registry"
```

---

### Task 2: Node-side registration handshake (`mycelium/node/registration.py`)

**Files:**
- Create: `src/mycelium/node/registration.py`
- Test: `tests/node/test_registration.py`

**Interfaces:**
- Consumes: nothing from other tasks — independent of Task 1.
- Produces: `REGISTRATION_TIMEOUT_SECONDS = 10.0`, `class RegistrationError(Exception)`, `class RegistrationRejected(RegistrationError)`, `class RegistrationTimeout(RegistrationError)`, `async def register(websocket, token: str, model: str, node_id: str, timeout: float = REGISTRATION_TIMEOUT_SECONDS) -> None`. Consumed by Task 3 (fixing the existing integration test), Task 5 (`node/cli.py`), and Task 6 (`status_cli.py` tests).

- [ ] **Step 1: Write the failing tests**

Create `tests/node/test_registration.py`:

```python
"""Tests for mycelium.node.registration."""

import asyncio
import json
import ssl

import pytest
import websockets

from mycelium.coordinator import certs
from mycelium.node import connection, registration


def _server_ssl_context(cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


async def test_register_succeeds_and_returns_on_registered_response(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    received = {}

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await registration.register(
                ws, token="secret", model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a"
            )

    assert received == {
        "type": "register",
        "token": "secret",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "node_id": "node-a",
    }


async def test_register_raises_rejected_on_registration_rejected_response(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async def fake_coordinator(websocket):
        await websocket.recv()
        await websocket.send(json.dumps({"type": "registration_rejected", "reason": "invalid token"}))

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            with pytest.raises(registration.RegistrationRejected, match="invalid token"):
                await registration.register(ws, token="bad", model="m", node_id="node-a")


async def test_register_raises_timeout_when_coordinator_never_responds(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async def silent_coordinator(websocket):
        await websocket.recv()
        await asyncio.sleep(60)  # never responds within the test's short timeout

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(silent_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            with pytest.raises(registration.RegistrationTimeout):
                await registration.register(ws, token="secret", model="m", node_id="node-a", timeout=0.5)


async def test_register_raises_on_unexpected_response_type(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async def confused_coordinator(websocket):
        await websocket.recv()
        await websocket.send(json.dumps({"type": "something_else"}))

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(confused_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            with pytest.raises(registration.RegistrationRejected):
                await registration.register(ws, token="secret", model="m", node_id="node-a")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_registration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.node.registration'`

- [ ] **Step 3: Write the implementation**

Create `src/mycelium/node/registration.py`:

```python
"""Sends the node's registration handshake to the coordinator and awaits
the result.

See the design doc for issue #8. This module owns exactly one exchange:
send {"type": "register", ...}, wait for {"type": "registered"} or
{"type": "registration_rejected", ...}, bounded by a timeout. Everything
after that — holding the connection open, future heartbeat/routing
messages — is connection.py's/cli.py's job, not this module's.
"""

from __future__ import annotations

import asyncio
import json

REGISTRATION_TIMEOUT_SECONDS = 10.0


class RegistrationError(Exception):
    """Base class for registration failures (rejected or timed out)."""


class RegistrationRejected(RegistrationError):
    """Raised when the coordinator rejects the registration, or responds
    with something other than a clear success."""


class RegistrationTimeout(RegistrationError):
    """Raised when the coordinator doesn't respond within the timeout."""


async def register(
    websocket,
    token: str,
    model: str,
    node_id: str,
    timeout: float = REGISTRATION_TIMEOUT_SECONDS,
) -> None:
    """Send the registration message and wait for the coordinator's
    response. Returns normally on success. Raises RegistrationRejected if
    the coordinator rejects the token (or responds unexpectedly), or
    RegistrationTimeout if no response arrives in time."""
    await websocket.send(
        json.dumps({"type": "register", "token": token, "model": model, "node_id": node_id})
    )
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
    except TimeoutError:
        raise RegistrationTimeout(f"coordinator did not respond within {timeout}s")

    message = json.loads(raw)
    if message.get("type") == "registered":
        return
    if message.get("type") == "registration_rejected":
        raise RegistrationRejected(message.get("reason", "unknown reason"))
    raise RegistrationRejected(f"unexpected response from coordinator: {message!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_registration.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/registration.py tests/node/test_registration.py
git commit -m "feat: node-side registration handshake"
```

---

### Task 3: Wire the registration protocol into the coordinator server

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Modify: `tests/coordinator/test_server.py`
- Modify: `tests/test_integration.py`

**Interfaces:**
- Consumes: `NodeRegistry` from Task 1; `registration.register`/`REGISTRATION_TIMEOUT_SECONDS` from Task 2 (used only in the `test_integration.py` fix, to drive a real node-side registration in that end-to-end test).
- Produces: `server.serve(host, port, cert_path, key_path, token: str)` (signature changed — every existing caller must be updated), `server.FIRST_MESSAGE_TIMEOUT_SECONDS`. Consumed by Task 4 (`coordinator/cli.py`) and Task 6 (`status_cli.py` tests).

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `tests/coordinator/test_server.py`:

```python
"""Tests for mycelium.coordinator.server."""

import asyncio
import json
import ssl

import pytest
import websockets

from mycelium.coordinator import certs, server


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def test_node_can_connect_over_tls(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            assert ws.state.name == "OPEN"


async def test_multiple_nodes_can_connect_simultaneously(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws1:
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws2:
                assert ws1.state.name == "OPEN"
                assert ws2.state.name == "OPEN"


async def test_connection_with_wrong_pinned_cert_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    other_cert_path = tmp_path / "other-cert.pem"
    other_key_path = tmp_path / "other-key.pem"
    certs.ensure_cert(other_cert_path, other_key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        wrong_ctx = _client_ssl_context(other_cert_path)
        try:
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=wrong_ctx):
                assert False, "expected connection to be rejected"
        except ssl.SSLCertVerificationError:
            pass


async def test_server_survives_abnormal_disconnect(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        assert ws.state.name == "OPEN"
        ws.transport.close()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws2:
            assert ws2.state.name == "OPEN"


async def test_valid_registration_is_accepted(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            response = json.loads(await ws.recv())
            assert response == {"type": "registered"}


async def test_registration_with_invalid_token_is_rejected_and_closed(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "register", "token": "wrong", "model": "m", "node_id": "node-a"}
            ))
            response = json.loads(await ws.recv())
            assert response["type"] == "registration_rejected"
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_registration_with_missing_token_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps({"type": "register", "model": "m", "node_id": "node-a"}))
            response = json.loads(await ws.recv())
            assert response["type"] == "registration_rejected"


async def test_registered_node_appears_in_status_query(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps({
                "type": "register",
                "token": "secret-token",
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "node_id": "node-a",
            }))
            await node_ws.recv()  # consume the "registered" ack

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response == {
                    "type": "status",
                    "nodes": [{"node_id": "node-a", "model": "Qwen/Qwen2.5-7B-Instruct"}],
                }


async def test_disconnected_node_is_removed_from_registry(tmp_path):
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
        await node_ws.recv()
        await node_ws.close()
        await asyncio.sleep(0.2)  # let the coordinator notice the disconnect

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []


async def test_duplicate_node_id_replaces_and_closes_old_connection(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        old_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await old_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "model-a", "node_id": "node-a"}
        ))
        await old_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_ws:
            await new_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "model-b", "node_id": "node-a"}
            ))
            await new_ws.recv()

            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await old_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"] == [{"node_id": "node-a", "model": "model-b"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_server.py -v`
Expected: FAIL — every test errors with `TypeError: serve() takes 4 positional arguments but 5 were given` (the `"secret-token"` argument doesn't exist yet), and the new registration/status tests would additionally fail on protocol behavior once the signature is fixed.

- [ ] **Step 3: Write the implementation**

Replace the full contents of `src/mycelium/coordinator/server.py`:

```python
"""WebSocket server the coordinator runs to accept dial-out node connections.

See ADR-0002 for why nodes dial out rather than the coordinator dialing in.
Handles the registration handshake and registry status queries — see the
design doc for issue #8. Heartbeat/liveness tracking beyond registration
(#9) and routing a client request (#10) are not this module's job yet.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path

import websockets

from mycelium.coordinator.registry import NodeRegistry

PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20
FIRST_MESSAGE_TIMEOUT_SECONDS = 10.0


async def _handle_node(websocket, registry: NodeRegistry) -> None:
    """Read the first message (a registration or a status query) and
    dispatch on it. A registered node's connection is then held open with
    no further business logic yet (that's #9/#10's job); a status query
    gets one response and the connection closes. Anything else — no
    message within the timeout, malformed JSON, an unrecognized type —
    closes the connection."""
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=FIRST_MESSAGE_TIMEOUT_SECONDS)
    except (TimeoutError, websockets.exceptions.ConnectionClosed):
        return

    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.close()
        return

    message_type = message.get("type")

    if message_type == "status_query":
        await _handle_status_query(websocket, registry, message)
        return

    if message_type == "register":
        await _handle_registration(websocket, registry, message)
        return

    await websocket.close()


async def _handle_status_query(websocket, registry: NodeRegistry, message: dict) -> None:
    if not registry.check_token(message.get("token", "")):
        await websocket.close()
        return
    await websocket.send(json.dumps({"type": "status", "nodes": registry.list_nodes()}))
    await websocket.close()


async def _handle_registration(websocket, registry: NodeRegistry, message: dict) -> None:
    if not registry.check_token(message.get("token", "")):
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "invalid or missing token"}
        ))
        await websocket.close()
        return

    node_id = message.get("node_id")
    model = message.get("model")
    if not node_id or not model:
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "node_id and model are required"}
        ))
        await websocket.close()
        return

    superseded = registry.register(node_id, model, websocket)
    if superseded is not None:
        await superseded.websocket.close()

    await websocket.send(json.dumps({"type": "registered"}))

    try:
        async for _message in websocket:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        registry.unregister(node_id, websocket)


def build_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Build the server-side TLS context from the coordinator's cert/key pair."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def serve(host: str, port: int, cert_path: Path, key_path: Path, token: str):
    """Start the coordinator's node-facing WebSocket server.

    Returns whatever `websockets.serve` returns: awaitable to get a `Server`
    instance directly, or usable as `async with serve(...) as server:`.
    """
    ssl_context = build_ssl_context(cert_path, key_path)
    registry = NodeRegistry(token)

    async def handler(websocket):
        await _handle_node(websocket, registry)

    return websockets.serve(
        handler,
        host,
        port,
        ssl=ssl_context,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_server.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Fix the existing end-to-end integration test**

`tests/test_integration.py`'s `test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop` calls `server.serve(...)` (now requires a `token` argument) and drives a raw node loop via `connection.connect(...)` directly, which never sends a registration message — `_handle_node` will now time out waiting for one and close the connection, breaking this test's `connect_count` assertions. Fix it to register on each connection, matching the new protocol, while preserving its original "survives reconnects" intent.

Replace the full contents of `tests/test_integration.py`:

```python
"""End-to-end local integration test: cert generation, server, and the node's
reconnecting client, all together. Real two-machine verification (a real
coordinator host and real node hardware) happens separately — see the
design doc and plan for issue #5 — this test only proves the pieces wire
up correctly on localhost before that.
"""

import asyncio

import websockets

from mycelium.coordinator import certs, server
from mycelium.node import connection, registration


async def test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    def fast_delays():
        while True:
            yield 0.1

    connect_count = 0
    stop = asyncio.Event()

    async def node_loop(port):
        nonlocal connect_count
        async for websocket in connection.connect(
            f"wss://127.0.0.1:{port}", cert_path, reconnect_delays_factory=fast_delays
        ):
            connect_count += 1
            await registration.register(websocket, token="secret-token", model="m", node_id="node-a")
            try:
                await websocket.wait_closed()
            except websockets.exceptions.ConnectionClosed:
                if stop.is_set():
                    return
                continue

    coordinator1 = await server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token")
    port = coordinator1.sockets[0].getsockname()[1]
    node_task = asyncio.create_task(node_loop(port))

    await asyncio.sleep(0.5)
    assert connect_count == 1, "node should have connected once to the first coordinator"

    coordinator1.close()
    await coordinator1.wait_closed()
    await asyncio.sleep(0.5)  # let the node notice the drop

    coordinator2 = await server.serve("127.0.0.1", port, cert_path, key_path, "secret-token")
    await asyncio.sleep(0.5)  # let the node reconnect

    stop.set()
    node_task.cancel()
    coordinator2.close()
    await coordinator2.wait_closed()

    assert connect_count == 2, f"expected exactly 2 connect attempts, got {connect_count}"


def test_server_and_connection_agree_on_keepalive_settings():
    assert server.PING_INTERVAL_SECONDS == connection.PING_INTERVAL_SECONDS
    assert server.PING_TIMEOUT_SECONDS == connection.PING_TIMEOUT_SECONDS


def test_server_and_registration_agree_on_timeout_settings():
    assert server.FIRST_MESSAGE_TIMEOUT_SECONDS == registration.REGISTRATION_TIMEOUT_SECONDS
```

Note: `coordinator2` re-binds the *same* port the first coordinator used (`coordinator1`'s assigned port, captured once) rather than a second dynamic `0` — the test's whole point is the node reconnecting to the same address after a drop, so the second `server.serve` call must reuse `port`, not get a new one.

- [ ] **Step 6: Run the full test suite**

Run: `pytest -v`
Expected: PASS, all tests across `tests/coordinator/`, `tests/node/`, and `tests/test_integration.py`.

- [ ] **Step 7: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py tests/test_integration.py
git commit -m "feat: wire registration handshake and status queries into the coordinator server"
```

---

### Task 4: Wire `--token-file` into the coordinator CLI

**Files:**
- Modify: `src/mycelium/coordinator/cli.py`
- Modify: `tests/coordinator/test_cli.py`

**Interfaces:**
- Consumes: `server.serve(host, port, cert_path, key_path, token)` from Task 3.
- Produces: nothing new consumed elsewhere — `mycelium-coordinator`'s CLI contract.

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `tests/coordinator/test_cli.py`:

```python
"""Tests for mycelium.coordinator.cli."""

import pytest

from mycelium.coordinator.cli import parse_args, _run


def test_parse_args_defaults(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    args = parse_args(["--token-file", str(token_file)])
    assert args.host == "0.0.0.0"
    assert args.port == 8765


def test_parse_args_overrides(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    args = parse_args(
        ["--host", "127.0.0.1", "--port", "9000", "--token-file", str(token_file)]
    )
    assert args.host == "127.0.0.1"
    assert args.port == 9000


def test_parse_args_requires_token_file():
    with pytest.raises(SystemExit):
        parse_args([])


async def test_run_requires_cert_san_ip_when_no_existing_cert(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    args = parse_args(
        [
            "--cert-file", str(tmp_path / "cert.pem"),
            "--key-file", str(tmp_path / "key.pem"),
            "--token-file", str(token_file),
        ]
    )
    with pytest.raises(SystemExit):
        await _run(args)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_cli.py -v`
Expected: FAIL — `test_parse_args_requires_token_file` fails because `parse_args([])` currently succeeds (no `--token-file` argument exists yet); the other three fail with `error: unrecognized arguments: --token-file ...`.

- [ ] **Step 3: Write the implementation**

Replace the full contents of `src/mycelium/coordinator/cli.py`:

```python
"""CLI entry point for the Mycelium coordinator."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from mycelium import __version__
from mycelium.coordinator import certs, server

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_CERT_PATH = Path.home() / ".mycelium" / "coordinator-cert.pem"
DEFAULT_KEY_PATH = Path.home() / ".mycelium" / "coordinator-key.pem"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-coordinator")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cert-file", type=Path, default=DEFAULT_CERT_PATH)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_PATH)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--cert-san-ip",
        default=None,
        help=(
            "IP address to embed in the auto-generated cert's Subject "
            "Alternative Name. Required the first time, when --cert-file/"
            "--key-file don't exist yet."
        ),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    if not (args.cert_file.exists() and args.key_file.exists()):
        if not args.cert_san_ip:
            raise SystemExit(
                "--cert-san-ip is required to generate a new cert "
                f"(no existing cert found at {args.cert_file})"
            )
        certs.ensure_cert(args.cert_file, args.key_file, args.cert_san_ip)

    token = args.token_file.read_text().strip()

    print(
        f"mycelium-coordinator {__version__} listening on {args.host}:{args.port}",
        flush=True,
    )
    async with server.serve(args.host, args.port, args.cert_file, args.key_file, token):
        await asyncio.Future()  # run forever


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_cli.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS, everything green.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/coordinator/cli.py tests/coordinator/test_cli.py
git commit -m "feat: coordinator reads its shared token from --token-file"
```

---

### Task 5: Wire registration into the node CLI

**Files:**
- Modify: `src/mycelium/node/cli.py`
- Modify: `tests/node/test_cli.py`

**Interfaces:**
- Consumes: `registration.register`/`RegistrationError` from Task 2; `server.serve`, `NodeRegistry`-backed protocol from Task 3 (used only by the new tests, via a real coordinator).
- Produces: nothing new consumed elsewhere — `mycelium-node`'s CLI contract and `_run`'s registration behavior.

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `tests/node/test_cli.py`:

```python
"""Tests for mycelium.node.cli."""

import asyncio
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
import websockets

from mycelium.coordinator import certs
from mycelium.node import vllm_process
from mycelium.node.cli import _run, parse_args

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parse_args_partial_coordinator_args_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--coordinator-cert", "/tmp/cert.pem"])
    with pytest.raises(SystemExit):
        parse_args(["--coordinator-url", "wss://example:8765"])


def test_parse_args_requires_coordinator_or_prompt():
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_prompt_alone_is_valid():
    args = parse_args(["--prompt", "hello"])
    assert args.prompt == "hello"
    assert args.coordinator_url is None
    assert args.coordinator_cert is None
    assert args.token_file is None


def test_parse_args_coordinator_requires_token_file(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    with pytest.raises(SystemExit):
        parse_args(
            ["--coordinator-url", "wss://example:8765", "--coordinator-cert", str(cert_path)]
        )


def test_parse_args_coordinator_alone_is_valid(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    args = parse_args(
        [
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == str(cert_path)
    assert args.prompt is None


def test_parse_args_defaults():
    args = parse_args(["--prompt", "hi"])
    assert args.model == vllm_process.DEFAULT_MODEL
    assert args.gpu == vllm_process.DEFAULT_GPU
    assert args.vllm_port == vllm_process.DEFAULT_PORT
    assert args.node_id is None


def test_parse_args_overrides():
    args = parse_args(
        [
            "--prompt", "hi",
            "--model", "some/other-model",
            "--gpu", "1",
            "--vllm-port", "9000",
            "--node-id", "my-node",
        ]
    )
    assert args.model == "some/other-model"
    assert args.gpu == "1"
    assert args.vllm_port == 9000
    assert args.node_id == "my-node"


class _FakeVLLMHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            body = json.dumps(
                {"choices": [{"message": {"content": "fake completion"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture
def fake_vllm_server():
    server = HTTPServer(("127.0.0.1", 0), _FakeVLLMHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_run_prompt_mode_forwards_prompt_and_prints_completion(
    monkeypatch, capsys, fake_vllm_server
):
    port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    args = parse_args(["--prompt", "what is the answer?", "--vllm-port", str(port)])
    process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    await _run(args, process)

    assert "fake completion" in capsys.readouterr().out


def _server_ssl_context(cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


async def test_run_registers_with_coordinator_using_token_and_node_id(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    token_file = tmp_path / "token"
    token_file.write_text("secret-token\n")

    received = {}
    registered_event = asyncio.Event()

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))
        registered_event.set()
        await websocket.wait_closed()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--token-file", str(token_file),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(_run(args, process))
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert received == {
        "type": "register",
        "token": "secret-token",
        "model": vllm_process.DEFAULT_MODEL,
        "node_id": "test-node",
    }


async def test_run_retries_after_registration_rejected(tmp_path, monkeypatch, fake_vllm_server):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    token_file = tmp_path / "token"
    token_file.write_text("wrong-token\n")

    attempt_count = 0

    async def rejecting_coordinator(websocket):
        nonlocal attempt_count
        attempt_count += 1
        await websocket.recv()
        await websocket.send(json.dumps({"type": "registration_rejected", "reason": "invalid token"}))
        await websocket.close()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(rejecting_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--token-file", str(token_file),
                "--vllm-port", str(vllm_port),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(_run(args, process))
        await asyncio.sleep(2.5)  # let it attempt, get rejected, back off (~1s), attempt again
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert attempt_count >= 2


def test_sigterm_stops_vllm_process_group_with_no_orphans(tmp_path):
    """Regression test for the SIGTERM/SIGHUP orphan bug found in final
    review: drives the real CLI as an OS subprocess (not a direct function
    call) so an actual signal is what triggers cleanup, via a `vllm` shim
    on PATH that spawns a child process the way vLLM spawns its own worker
    — proving a bare-PID kill would leave that child behind."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()

    shim = bin_dir / "vllm"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import http.server, os, subprocess, sys\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "pid_dir = os.environ['FAKE_VLLM_PID_DIR']\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "open(os.path.join(pid_dir, 'parent'), 'w').write(str(os.getpid()))\n"
        "open(os.path.join(pid_dir, 'child'), 'w').write(str(child.pid))\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    shim.chmod(0o755)

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    token_file = tmp_path / "token"
    token_file.write_text("unused-token\n")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_VLLM_PID_DIR"] = str(pid_dir)
    vllm_port = _free_port()

    node_proc = subprocess.Popen(
        [
            sys.executable, "-m", "mycelium.node.cli",
            "--coordinator-url", "wss://127.0.0.1:1",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--vllm-port", str(vllm_port),
        ],
        env=env,
    )
    parent_pid = None
    child_pid = None
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if (pid_dir / "parent").exists() and (pid_dir / "child").exists():
                break
            time.sleep(0.2)
        else:
            pytest.fail("fake vllm never reported its PIDs")

        parent_pid = int((pid_dir / "parent").read_text())
        child_pid = int((pid_dir / "child").read_text())
        assert _process_alive(parent_pid)
        assert _process_alive(child_pid)

        node_proc.send_signal(signal.SIGTERM)
        node_proc.wait(timeout=15.0)
    finally:
        if node_proc.poll() is None:
            node_proc.kill()
            node_proc.wait()

    assert parent_pid is not None and child_pid is not None
    time.sleep(0.5)
    assert not _process_alive(parent_pid)
    assert not _process_alive(child_pid)
```

Note what changed versus the previous version of this file: `test_parse_args_coordinator_alone_is_valid` now also passes `--token-file` (previously it didn't need to); a new `test_parse_args_coordinator_requires_token_file` covers the new validation; `test_sigterm_stops_vllm_process_group_with_no_orphans` now also passes `--token-file` (any readable file — that connection never reaches the registration step, since it targets an unreachable address).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_cli.py -v`
Expected: several failures — `test_parse_args_coordinator_requires_token_file` fails (no such validation yet); `test_parse_args_coordinator_alone_is_valid` fails with `AttributeError` or `unrecognized arguments: --token-file`; `test_parse_args_defaults`/`test_parse_args_overrides` fail on the new `node_id`/`--node-id` assertions; `test_run_registers_with_coordinator_using_token_and_node_id` and `test_run_retries_after_registration_rejected` fail because `_run` doesn't attempt registration at all yet; `test_sigterm_stops_vllm_process_group_with_no_orphans` fails with `unrecognized arguments: --token-file`.

- [ ] **Step 3: Write the implementation**

Replace the full contents of `src/mycelium/node/cli.py`:

```python
"""CLI entry point for the Mycelium node agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
from pathlib import Path

from mycelium import __version__
from mycelium.node import connection, registration
from mycelium.node.vllm_process import (
    DEFAULT_GPU,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    VLLMProcess,
    VLLMReadyTimeout,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-node")
    parser.add_argument("--coordinator-url", default=None)
    parser.add_argument("--coordinator-cert", type=Path, default=None)
    parser.add_argument("--token-file", type=Path, default=None)
    parser.add_argument("--node-id", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gpu", default=DEFAULT_GPU)
    parser.add_argument("--vllm-port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--prompt",
        default=None,
        help="Send this one prompt to vLLM and exit, without connecting to a coordinator.",
    )
    args = parser.parse_args(argv)

    has_url = args.coordinator_url is not None
    has_cert = args.coordinator_cert is not None
    if has_url != has_cert:
        parser.error("--coordinator-url and --coordinator-cert must be given together")
    if not (has_url and has_cert) and args.prompt is None:
        parser.error("either --coordinator-url/--coordinator-cert or --prompt is required")
    if has_url and args.token_file is None:
        parser.error("--token-file is required when connecting to a coordinator")

    return args


async def _run(args: argparse.Namespace, process: VLLMProcess) -> None:
    print(f"starting vLLM ({args.model} on GPU {args.gpu})...", flush=True)
    await asyncio.to_thread(process.start)
    try:
        await asyncio.to_thread(process.wait_ready)
        print("vLLM ready", flush=True)

        if args.prompt is not None:
            result = await asyncio.to_thread(process.complete, args.prompt)
            print(result, flush=True)
            return

        token = args.token_file.read_text().strip()
        node_id = args.node_id or socket.gethostname()

        print(f"mycelium-node {__version__} connecting to {args.coordinator_url}", flush=True)
        async for websocket in connection.connect(args.coordinator_url, args.coordinator_cert):
            print(f"connected to coordinator ({args.coordinator_url})", flush=True)
            try:
                await registration.register(websocket, token=token, model=args.model, node_id=node_id)
                print(f"registered with coordinator as {node_id!r}", flush=True)
            except registration.RegistrationError as exc:
                print(f"registration failed: {exc}", flush=True)
                await websocket.close()
                continue
            try:
                await websocket.wait_closed()
                print("connection to coordinator closed, reconnecting...", flush=True)
            except Exception:
                continue
    finally:
        await asyncio.to_thread(process.stop)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    process = VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    # Raw signal.signal, not asyncio's loop.add_signal_handler: a real OS
    # signal interrupts the event loop's blocking wait even while the main
    # coroutine is stuck inside an `asyncio.to_thread(...)` call (e.g. mid
    # `wait_ready`, which can block up to READY_TIMEOUT_SECONDS) — asyncio
    # task cancellation cannot interrupt an already-running executor thread,
    # so only a synchronous, process-level handler reliably stops vLLM here.
    def _handle_signal(signum: int, _frame) -> None:
        process.stop()
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    try:
        asyncio.run(_run(args, process))
    except VLLMReadyTimeout as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_cli.py -v`
Expected: PASS (11 tests). `test_run_retries_after_registration_rejected` and `test_sigterm_stops_vllm_process_group_with_no_orphans` are the slowest (real backoff timing / real subprocess + signal), a few seconds each — that's expected, not a hang.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS, everything green, no leftover processes afterward (`ps aux | grep -iE 'fake_vllm|time.sleep\(600\)'` shows nothing).

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/node/cli.py tests/node/test_cli.py
git commit -m "feat: node agent registers with the coordinator on connect"
```

---

### Task 6: `mycelium-coordinator-status` query command

**Files:**
- Create: `src/mycelium/coordinator/status_cli.py`
- Modify: `pyproject.toml`
- Test: `tests/coordinator/test_status_cli.py`

**Interfaces:**
- Consumes: `server.serve` from Task 3; `registration.register` from Task 2 (test-only, to register a fake node before querying); `connection.build_ssl_context` (existing, from #5).
- Produces: `query_status(coordinator_url: str, coordinator_cert: Path, token: str) -> list[dict]`, `parse_args`, `main` — the `mycelium-coordinator-status` entry point. Nothing consumed by other tasks — last task in this plan.

- [ ] **Step 1: Write the failing tests**

Create `tests/coordinator/test_status_cli.py`:

```python
"""Tests for mycelium.coordinator.status_cli."""

import ssl

import pytest
import websockets

from mycelium.coordinator import certs, server
from mycelium.coordinator.status_cli import parse_args, query_status
from mycelium.node import connection, registration


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


def test_parse_args_requires_all_three_flags():
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_valid(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    args = parse_args(
        [
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == str(cert_path)
    assert str(args.token_file) == str(token_file)


async def test_query_status_returns_empty_list_when_no_nodes_registered(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        nodes = await query_status(f"wss://127.0.0.1:{port}", cert_path, "secret-token")

    assert nodes == []


async def test_query_status_returns_registered_node(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await registration.register(
                node_ws, token="secret-token", model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a"
            )

            nodes = await query_status(f"wss://127.0.0.1:{port}", cert_path, "secret-token")

    assert nodes == [{"node_id": "node-a", "model": "Qwen/Qwen2.5-7B-Instruct"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_status_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.coordinator.status_cli'`

- [ ] **Step 3: Write the implementation**

Create `src/mycelium/coordinator/status_cli.py`:

```python
"""CLI entry point for querying the coordinator's node registry.

See the design doc for issue #8. Connects like a node would (same TLS
cert, same shared token) but as a one-shot request/response, not a
long-lived connection — this is an operator tool, not a node agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import websockets

from mycelium.node.connection import build_ssl_context


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-coordinator-status")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    return parser.parse_args(argv)


async def query_status(coordinator_url: str, coordinator_cert: Path, token: str) -> list[dict]:
    """Connect, ask for the current registry, and return the node list."""
    ssl_context = build_ssl_context(coordinator_cert)
    async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
        await websocket.send(json.dumps({"type": "status_query", "token": token}))
        raw = await websocket.recv()
        message = json.loads(raw)
        return message.get("nodes", [])


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    nodes = asyncio.run(query_status(args.coordinator_url, args.coordinator_cert, token))
    if not nodes:
        print("No nodes registered.")
        return
    for node in nodes:
        print(f"{node['node_id']}: {node['model']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add the new script to `pyproject.toml`**

In `pyproject.toml`, find:

```toml
[project.scripts]
mycelium-node = "mycelium.node.cli:main"
mycelium-coordinator = "mycelium.coordinator.cli:main"
mycelium-client = "mycelium.client.cli:main"
```

Replace it with:

```toml
[project.scripts]
mycelium-node = "mycelium.node.cli:main"
mycelium-coordinator = "mycelium.coordinator.cli:main"
mycelium-coordinator-status = "mycelium.coordinator.status_cli:main"
mycelium-client = "mycelium.client.cli:main"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_status_cli.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Reinstall the package so the new script is registered, and verify it**

Run:

```bash
uv pip install --python .venv/bin/python -e .
.venv/bin/mycelium-coordinator-status --help
```

Expected: a usage message listing `--coordinator-url`, `--coordinator-cert`, `--token-file` — confirms `pyproject.toml`'s new `[project.scripts]` entry actually installed.

- [ ] **Step 7: Run the full test suite**

Run: `pytest -v`
Expected: PASS, everything green — this closes out all three of issue #8's acceptance criteria together (registration message with token+model, invalid/missing token rejected, registered node visible via `mycelium-coordinator-status`).

- [ ] **Step 8: Commit**

```bash
git add src/mycelium/coordinator/status_cli.py pyproject.toml tests/coordinator/test_status_cli.py
git commit -m "feat: mycelium-coordinator-status registry query command"
```

---

## After this plan: real-hardware verification (not a subagent task)

Once all six tasks are merged, the orchestrating agent runs the full loop for real: a real coordinator on a reachable host (or `a6000` itself, dual-purposed) with a generated `--token-file`, and a real `mycelium-node` on `a6000` connecting with the same token — confirming the node appears via `mycelium-coordinator-status`, and confirming a wrong/missing token is cleanly rejected. That narrative gets added to `docs/superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md` as a new "Live verification" section, following issues #6/#7's precedent — not delegated to a subagent, for the same reason those issues' live-hardware steps weren't either.
