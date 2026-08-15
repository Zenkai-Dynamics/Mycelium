# Issue #10 — Coordinator Forwards a Client Request to a Healthy Node — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A client sends a prompt to the coordinator's one stable address; the coordinator picks a healthy node registered for the requested model and forwards the request over that node's already-open connection; the node runs it against its local vLLM and the completion comes back to the client unchanged. This closes Phase 0 success criterion #2.

**Architecture:** Reuses the existing single TLS WebSocket server (no new HTTP/ASGI surface) with a new `"complete"` message type on both legs — client↔coordinator and coordinator↔node. A new `coordinator/router.py` owns picking a node's already-open connection and correlating a `request_id`-tagged reply to it via an `asyncio.Future`; a new `node/request_handler.py` owns turning an incoming `"complete"` message into a threaded call to the already-built `VLLMProcess.complete()` and a reply. `client/cli.py` (currently a no-op stub) becomes a one-shot CLI symmetric with `mycelium-coordinator-status`.

**Tech Stack:** Python 3.11+, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`, already configured), `websockets` 17.x (already pinned). No new dependencies.

## Global Constraints

- **Source of truth:** `docs/superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md`. Every decision below traces to a section there — if anything in this plan seems to contradict it, the design doc wins; stop and re-read it rather than guessing.
- No new HTTP/ASGI surface. The client is another one-shot `websockets.connect` caller on the coordinator's existing TLS port, exactly like `mycelium-coordinator-status`.
- Client authentication reuses the same shared token nodes use (`registry.check_token`, `hmac.compare_digest`) — no new credential type.
- The client's request **requires** `"model"` — no implicit/default model selection.
- Node selection is first-registered-match only (`NodeRegistry.find_node_for_model`) — no load balancing, no queueing, no retries. "Healthy" means "currently in the registry" (already established by #9 — no separate health check).
- Timeouts, chosen so the most specific error wins the race: `node/vllm_process.py`'s existing `COMPLETE_TIMEOUT_SECONDS = 120.0` (unchanged) < new `coordinator/router.py`'s `NODE_COMPLETE_TIMEOUT_SECONDS = 130.0` < new `client/cli.py`'s `CLIENT_COMPLETE_TIMEOUT_SECONDS = 140.0`.
- Correlation: every coordinator↔node message carries a `request_id` (`str(uuid.uuid4())`), so concurrent client requests routed to the same node's single connection never get their replies crossed.
- A node connection's pending requests must be tracked via a **captured** `Node` reference (fetched once, immediately after `registry.register(...)`, via a new `registry.get(node_id)`) — never re-looked-up from the registry later. This is what keeps cleanup correct when a node reconnects (duplicate `node_id`, #8's replace-and-close-old-connection behavior) while its old connection still has requests in flight.
- Node-side concurrent request handling is uncapped for Phase 0 (a task per incoming `"complete"` message, no semaphore) — deliberate, not an oversight; see the design doc's out-of-scope section.
- No queueing, retries, or failover anywhere in this issue (`phase-0-foundation.md`, unchanged).
- This issue's final task is **live-hardware verification** (unlike #9's simulated-only exception) — do not mark the issue done from simulated tests alone.

---

### Task 1: Registry additions — `find_node_for_model`, `get`, and a `pending` field on `Node`

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Modify: `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `NodeRegistry.find_node_for_model(model: str) -> Node | None` (Task 3 consumes this). `NodeRegistry.get(node_id: str) -> Node | None` (Task 3 consumes this, to capture a stable `Node` reference right after registering). `Node.pending: dict[str, asyncio.Future]` (Tasks 2 and 3 consume this).

- [ ] **Step 1: Write the failing tests**

Add to `tests/coordinator/test_registry.py`, after the existing `test_check_token_rejects_non_string_token`:

```python
def test_find_node_for_model_returns_matching_node():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    node = registry.find_node_for_model("model-a")
    assert node is not None
    assert node.node_id == "node-a"


def test_find_node_for_model_returns_none_when_no_match():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    assert registry.find_node_for_model("model-b") is None


def test_find_node_for_model_returns_none_when_registry_empty():
    registry = NodeRegistry("secret")
    assert registry.find_node_for_model("model-a") is None


def test_find_node_for_model_returns_first_match_when_multiple_host_same_model():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    registry.register("node-b", "model-a", websocket="ws-b")
    node = registry.find_node_for_model("model-a")
    assert node.node_id == "node-a"


def test_get_returns_registered_node():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    node = registry.get("node-a")
    assert node is not None
    assert node.node_id == "node-a"
    assert node.websocket == "ws-a"


def test_get_returns_none_for_unknown_node_id():
    registry = NodeRegistry("secret")
    assert registry.get("node-a") is None


def test_new_node_has_empty_pending_dict():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    assert registry.get("node-a").pending == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_registry.py -v`
Expected: the seven new tests FAIL with `AttributeError: 'NodeRegistry' object has no attribute 'find_node_for_model'` (and similarly for `get`, and `pending` on `Node`).

- [ ] **Step 3: Implement**

In `src/mycelium/coordinator/registry.py`, replace:

```python
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any


@dataclass
class Node:
    node_id: str
    model: str
    websocket: Any
```

with:

```python
from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    node_id: str
    model: str
    websocket: Any
    # In-flight routed requests on this node's connection, keyed by
    # request_id — see the design doc for issue #10. Lives on the Node
    # itself (not a separate coordinator-wide dict) so it's tied to this
    # exact connection's lifetime: when the connection goes away, whoever
    # cleans it up already has this dict via the Node reference they
    # captured at registration time.
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
```

Then, after `unregister`, add:

```python
    def find_node_for_model(self, model: str) -> Node | None:
        """Return the first registered node hosting `model`, or None. No
        load balancing across same-model nodes — see the design doc for
        issue #10: Phase 0 doesn't need fairness, just a healthy match."""
        for node in self._nodes.values():
            if node.model == model:
                return node
        return None

    def get(self, node_id: str) -> Node | None:
        """Return the currently-registered Node for node_id, or None.

        Used by the connection-handling task right after it calls
        register(), to capture a stable reference to its own entry for
        later cleanup — see the design doc for issue #10 on why that
        reference must be captured once and never re-fetched later: a
        later re-fetch could return a *different* connection's Node if
        this one has since been superseded by a reconnect under the same
        node_id.
        """
        return self._nodes.get(node_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_registry.py -v`
Expected: all PASS, including the seven new ones.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS, no other test affected (this task only adds new methods/a new field with a default, no existing signature changed).

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: registry gains find_node_for_model, get, and per-node pending requests"
```

---

### Task 2: `coordinator/router.py` — route a request to a specific node and correlate the reply

**Files:**
- Create: `src/mycelium/coordinator/router.py`
- Create: `tests/coordinator/test_router.py`

**Interfaces:**
- Consumes: `mycelium.coordinator.registry.Node` (from Task 1) — specifically `node.websocket.send(str) -> Awaitable[None]` and `node.pending: dict[str, asyncio.Future]`.
- Produces: `RoutingError`, `NoHealthyNodeError`, `NodeTimeoutError`, `NodeDisconnectedError`, `NodeError` (all exception classes); `NODE_COMPLETE_TIMEOUT_SECONDS = 130.0`; `route_request(node: Node, prompt: str, timeout: float = NODE_COMPLETE_TIMEOUT_SECONDS) -> str` (async). Task 3 consumes all of these.

- [ ] **Step 1: Write the failing tests**

Create `tests/coordinator/test_router.py`:

```python
"""Tests for mycelium.coordinator.router."""

import asyncio
import json

import pytest
import websockets

from mycelium.coordinator import router
from mycelium.coordinator.registry import Node


class _FakeNodeWebsocket:
    """Stand-in for a node's websocket. route_request only ever calls
    send() on it — replies arrive by resolving node.pending directly, the
    same way server.py's real dispatch loop will in Task 3."""

    def __init__(self, send_raises=None):
        self.sent: list[str] = []
        self._send_raises = send_raises

    async def send(self, raw: str) -> None:
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append(raw)


def _make_node(websocket=None) -> Node:
    return Node(node_id="node-a", model="m", websocket=websocket or _FakeNodeWebsocket())


async def _resolve_after(node: Node, delay: float, message: dict) -> None:
    await asyncio.sleep(delay)
    (request_id,) = node.pending.keys()
    node.pending[request_id].set_result({**message, "request_id": request_id})


async def test_route_request_sends_a_complete_message_with_request_id():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result", "text": "hi"}))

    await router.route_request(node, "what's up?", timeout=2.0)

    assert len(node.websocket.sent) == 1
    sent = json.loads(node.websocket.sent[0])
    assert sent["type"] == "complete"
    assert sent["prompt"] == "what's up?"
    assert isinstance(sent["request_id"], str) and sent["request_id"]


async def test_route_request_returns_text_on_success():
    node = _make_node()
    asyncio.create_task(
        _resolve_after(node, 0.05, {"type": "complete_result", "text": "the answer"})
    )

    text = await router.route_request(node, "prompt", timeout=2.0)

    assert text == "the answer"


async def test_route_request_cleans_up_pending_on_success():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result", "text": "hi"}))

    await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


async def test_route_request_raises_node_error_when_node_reports_failure():
    node = _make_node()
    asyncio.create_task(
        _resolve_after(node, 0.05, {"type": "complete_error", "reason": "vLLM exploded"})
    )

    with pytest.raises(router.NodeError, match="vLLM exploded"):
        await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


async def test_route_request_raises_timeout_when_no_reply_arrives():
    node = _make_node()

    with pytest.raises(router.NodeTimeoutError):
        await router.route_request(node, "prompt", timeout=0.1)

    assert node.pending == {}


async def test_route_request_raises_disconnected_when_send_fails():
    websocket = _FakeNodeWebsocket(
        send_raises=websockets.exceptions.ConnectionClosedError(None, None)
    )
    node = _make_node(websocket)

    with pytest.raises(router.NodeDisconnectedError):
        await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


async def test_route_request_propagates_disconnected_error_set_on_future():
    node = _make_node()

    async def fail_soon():
        await asyncio.sleep(0.05)
        (request_id,) = node.pending.keys()
        node.pending[request_id].set_exception(
            router.NodeDisconnectedError("node disconnected mid-request")
        )

    asyncio.create_task(fail_soon())

    with pytest.raises(router.NodeDisconnectedError, match="disconnected mid-request"):
        await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


def test_all_router_errors_are_routing_errors():
    assert issubclass(router.NoHealthyNodeError, router.RoutingError)
    assert issubclass(router.NodeTimeoutError, router.RoutingError)
    assert issubclass(router.NodeDisconnectedError, router.RoutingError)
    assert issubclass(router.NodeError, router.RoutingError)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.coordinator.router'`.

- [ ] **Step 3: Implement**

Create `src/mycelium/coordinator/router.py`:

```python
"""Routes one client completion request to a specific node's already-open
coordinator connection and returns its response.

See the design doc for issue #10. This module owns exactly one exchange
per call: send {"type": "complete", "request_id", "prompt"} on the node's
websocket, wait for a correlated reply. Picking *which* node (or
discovering there isn't a healthy one) is the caller's job
(mycelium.coordinator.registry.find_node_for_model) — that's why
NoHealthyNodeError lives here as part of this feature's error taxonomy
but is never raised by route_request itself.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import websockets

from mycelium.coordinator.registry import Node

# 10s past node/vllm_process.py's own COMPLETE_TIMEOUT_SECONDS (120s), so
# the node's own vLLM-call timeout fires first and produces a specific
# complete_error, rather than the coordinator giving up first with a
# vaguer one.
NODE_COMPLETE_TIMEOUT_SECONDS = 130.0


class RoutingError(Exception):
    """Base class for a routed request failing, for any reason."""


class NoHealthyNodeError(RoutingError):
    """Raised by callers (not route_request itself) when no registered
    node hosts the requested model."""


class NodeTimeoutError(RoutingError):
    """Raised when a node accepted a routed request but never replied
    within the timeout."""


class NodeDisconnectedError(RoutingError):
    """Raised when the node's connection is (or becomes) unusable — either
    it was already closed when we tried to send, or it closed while we
    were waiting for a reply. A caller shouldn't need to (and can't)
    distinguish those two cases."""


class NodeError(RoutingError):
    """Raised when the node itself explicitly reports the completion
    failed (a complete_error reply — e.g. vLLM errored), as opposed to
    timing out or disconnecting."""


async def route_request(
    node: Node, prompt: str, timeout: float = NODE_COMPLETE_TIMEOUT_SECONDS
) -> str:
    """Send `prompt` to `node` over its already-open connection and return
    its completion text.

    Raises NodeDisconnectedError if the connection is or becomes unusable,
    NodeTimeoutError if no reply arrives within `timeout`, or NodeError if
    the node explicitly reports a failure. `node.pending` never retains an
    entry for this request once this function returns or raises.
    """
    request_id = str(uuid.uuid4())
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    node.pending[request_id] = future
    try:
        try:
            await node.websocket.send(
                json.dumps({"type": "complete", "request_id": request_id, "prompt": prompt})
            )
        except websockets.exceptions.ConnectionClosed as exc:
            raise NodeDisconnectedError(f"node {node.node_id!r} disconnected: {exc}") from exc

        try:
            async with asyncio.timeout(timeout):
                message = await future
        except TimeoutError:
            raise NodeTimeoutError(
                f"node {node.node_id!r} did not respond within {timeout}s"
            ) from None
    finally:
        node.pending.pop(request_id, None)

    if message.get("type") == "complete_result":
        return message["text"]
    # _dispatch_node_message (server.py) only ever resolves this future
    # with a "complete_result" or "complete_error" message — anything else
    # coming out of `await future` above is a set_exception, not this
    # branch — so this is always a complete_error at this point.
    raise NodeError(message.get("reason", "node reported a failure with no reason given"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_router.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/coordinator/router.py tests/coordinator/test_router.py
git commit -m "feat: coordinator/router.py routes a request to a specific node and correlates the reply"
```

---

### Task 3: Wire `"complete"` into `server.py` — client requests and the node-side dispatch loop

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Modify: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `registry.find_node_for_model`, `registry.get` (Task 1); `router.route_request`, `router.RoutingError` and subclasses, `router.NODE_COMPLETE_TIMEOUT_SECONDS` (Task 2).
- Produces: the coordinator now accepts `{"type": "complete", "token", "model", "prompt"}` from a client and replies `{"type": "complete_result", "text"}` or `{"type": "complete_error", "reason"}`. Task 6's client CLI and Task 7's integration test both consume this wire behavior; it needs no Python-level interface since nothing imports `_handle_complete_request` directly.

- [ ] **Step 1: Write the failing tests**

Add to `tests/coordinator/test_server.py`, after `test_silently_unresponsive_node_is_dropped_within_ping_timeout_window` (the last existing test in the file):

```python
async def _run_fake_node(node_ws, reply_for) -> None:
    """Stand-in for a real node agent: replies to every "complete" message
    it receives using reply_for(message) -> dict (the reply body, minus
    request_id — this helper fills that in)."""
    async for raw in node_ws:
        message = json.loads(raw)
        reply = reply_for(message)
        reply["request_id"] = message["request_id"]
        await node_ws.send(json.dumps(reply))


async def test_complete_request_routes_to_registered_node_and_returns_result(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_ws.recv()  # consume "registered"
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_result", "text": f"echo: {msg['prompt']}"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hello"}
                ))
                response = json.loads(await client_ws.recv())
                assert response == {"type": "complete_result", "text": "echo: hello"}

            node_task.cancel()


async def test_complete_request_with_no_matching_node_returns_error(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "no-such-model", "prompt": "hi"}
            ))
            response = json.loads(await client_ws.recv())
            assert response["type"] == "complete_error"
            assert "no-such-model" in response["reason"]


async def test_complete_request_with_wrong_token_is_closed_without_reply(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "wrong", "model": "m", "prompt": "hi"}
            ))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await client_ws.recv()


async def test_complete_request_with_missing_prompt_returns_error(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m"}
            ))
            response = json.loads(await client_ws.recv())
            assert response["type"] == "complete_error"


async def test_complete_request_node_reports_failure_is_relayed_to_client(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_error", "reason": "vLLM exploded"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())
                assert response == {"type": "complete_error", "reason": "vLLM exploded"}

            node_task.cancel()


async def test_complete_request_node_disconnect_mid_request_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
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

        async def receive_then_never_reply():
            await node_ws.recv()  # accept the routed "complete", then go silent

        node_task = asyncio.create_task(receive_then_never_reply())

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
            ))
            await node_task  # make sure the node has received the routed request
            await node_ws.close()  # simulate the node dying mid-request

            start = time.monotonic()
            response = json.loads(await asyncio.wait_for(client_ws.recv(), timeout=5.0))
            elapsed = time.monotonic() - start
            assert response["type"] == "complete_error"
            assert elapsed < 3.0, (
                f"client waited {elapsed:.2f}s — a node disconnect mid-request must fail "
                "fast, not wait out the full 30s NODE_COMPLETE_TIMEOUT_SECONDS"
            )


async def test_concurrent_complete_requests_to_same_node_get_correct_replies(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_ws.recv()

            async def flaky_reversing_node():
                # Reply out of arrival order, to prove correlation (not
                # send order) determines which client gets which answer.
                first = json.loads(await node_ws.recv())
                second = json.loads(await node_ws.recv())
                await node_ws.send(json.dumps({
                    "type": "complete_result", "request_id": second["request_id"],
                    "text": f"reply to: {second['prompt']}",
                }))
                await node_ws.send(json.dumps({
                    "type": "complete_result", "request_id": first["request_id"],
                    "text": f"reply to: {first['prompt']}",
                }))

            node_task = asyncio.create_task(flaky_reversing_node())

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_a:
                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_b:
                    await client_a.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "A"}
                    ))
                    await client_b.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "B"}
                    ))
                    response_a = json.loads(await client_a.recv())
                    response_b = json.loads(await client_b.recv())

            await node_task
            assert response_a == {"type": "complete_result", "text": "reply to: A"}
            assert response_b == {"type": "complete_result", "text": "reply to: B"}
```

At the top of `tests/coordinator/test_server.py`, update the import line:

```python
from mycelium.coordinator import certs, server
```

to:

```python
from mycelium.coordinator import certs, router, server
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_server.py -v -k complete_request or concurrent_complete`
Expected: FAIL — `"complete"` isn't handled by `_handle_node` yet (client requests get no reply, so these tests time out or get an unexpected response), and the node-side loop still discards everything (`async for _message in websocket: pass`), so a node never sees the routed `"complete"` message either.

- [ ] **Step 3: Implement**

In `src/mycelium/coordinator/server.py`, replace the module docstring and imports:

```python
"""WebSocket server the coordinator runs to accept dial-out node connections.

See ADR-0002 for why nodes dial out rather than the coordinator dialing in.
Handles the registration handshake and registry status queries — see the
design doc for issue #8. Node liveness is tracked via the WebSocket
ping/pong keepalive (PING_INTERVAL_SECONDS/PING_TIMEOUT_SECONDS/
CLOSE_TIMEOUT_SECONDS below) — see the design doc for issue #9: a node
that goes silent gets its connection closed by the `websockets` library
itself, which `_handle_registration`'s `finally: registry.unregister(...)`
already turns into a registry drop, the same as any other disconnect.
Routing a client request (#10) is not this module's job yet.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path

import websockets

from mycelium.coordinator.registry import NodeRegistry
```

with:

```python
"""WebSocket server the coordinator runs to accept dial-out node connections.

See ADR-0002 for why nodes dial out rather than the coordinator dialing in.
Handles the registration handshake and registry status queries — see the
design doc for issue #8. Node liveness is tracked via the WebSocket
ping/pong keepalive (PING_INTERVAL_SECONDS/PING_TIMEOUT_SECONDS/
CLOSE_TIMEOUT_SECONDS below) — see the design doc for issue #9: a node
that goes silent gets its connection closed by the `websockets` library
itself, which `_handle_registration`'s `finally: registry.unregister(...)`
already turns into a registry drop, the same as any other disconnect.
Routing a client request to a healthy node (#10) is handled by the
`"complete"` branch below and mycelium.coordinator.router.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path

import websockets

from mycelium.coordinator import router
from mycelium.coordinator.registry import Node, NodeRegistry
```

Replace the `_handle_node` dispatch function:

```python
async def _handle_node(websocket, registry: NodeRegistry) -> None:
    """Read the first message (a registration or a status query) and
    dispatch on it. A registered node's connection is then held open with
    no further business logic yet (that's #10's job); a status query
    gets one response and the connection closes. Anything else — no
    message within the timeout, malformed JSON, an unrecognized type —
    closes the connection."""
    try:
        # asyncio.timeout(), not asyncio.wait_for() — see registration.py's
        # matching comment: wait_for has a Python 3.11 cancellation race
        # this side is equally exposed to.
        async with asyncio.timeout(FIRST_MESSAGE_TIMEOUT_SECONDS):
            raw = await websocket.recv()
    except (TimeoutError, websockets.exceptions.ConnectionClosed):
        return

    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.close()
        return

    if not isinstance(message, dict):
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
```

with:

```python
async def _handle_node(websocket, registry: NodeRegistry) -> None:
    """Read the first message and dispatch on it: a node registration, a
    status query, or a client's completion request. A registered node's
    connection is then held open for routed requests (see
    _handle_registration below); a status query or completion request
    gets one response and the connection closes. Anything else — no
    message within the timeout, malformed JSON, an unrecognized type —
    closes the connection."""
    try:
        # asyncio.timeout(), not asyncio.wait_for() — see registration.py's
        # matching comment: wait_for has a Python 3.11 cancellation race
        # this side is equally exposed to.
        async with asyncio.timeout(FIRST_MESSAGE_TIMEOUT_SECONDS):
            raw = await websocket.recv()
    except (TimeoutError, websockets.exceptions.ConnectionClosed):
        return

    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.close()
        return

    if not isinstance(message, dict):
        await websocket.close()
        return

    message_type = message.get("type")

    if message_type == "status_query":
        await _handle_status_query(websocket, registry, message)
        return

    if message_type == "register":
        await _handle_registration(websocket, registry, message)
        return

    if message_type == "complete":
        await _handle_complete_request(websocket, registry, message)
        return

    await websocket.close()


async def _handle_complete_request(websocket, registry: NodeRegistry, message: dict) -> None:
    """A client's one-shot completion request: authenticate, pick a
    healthy node hosting the requested model, forward, relay the result
    (or a clear error) back, then close — see the design doc for issue
    #10."""
    if not registry.check_token(message.get("token")):
        await websocket.close()
        return

    model = message.get("model")
    prompt = message.get("prompt")
    if not model or not prompt:
        await websocket.send(json.dumps(
            {"type": "complete_error", "reason": "model and prompt are required"}
        ))
        await websocket.close()
        return

    try:
        node = registry.find_node_for_model(model)
        if node is None:
            raise router.NoHealthyNodeError(f"no healthy node for model {model!r}")
        text = await router.route_request(node, prompt)
    except router.RoutingError as exc:
        await websocket.send(json.dumps({"type": "complete_error", "reason": str(exc)}))
        await websocket.close()
        return

    await websocket.send(json.dumps({"type": "complete_result", "text": text}))
    await websocket.close()


def _dispatch_node_message(node: Node, raw: str) -> None:
    """Resolve the pending future for a complete_result/complete_error
    reply from a registered node. Anything else — malformed JSON, an
    unrecognized type, a request_id with no matching pending future (e.g.
    a very late reply after route_request already gave up) — is silently
    ignored: this connection has no other job than routed request/response
    after registration, and there's no client left to usefully report a
    problem to."""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return
    if message.get("type") not in ("complete_result", "complete_error"):
        return
    future = node.pending.get(message.get("request_id"))
    if future is not None and not future.done():
        future.set_result(message)
```

Replace `_handle_registration`'s body:

```python
async def _handle_registration(websocket, registry: NodeRegistry, message: dict) -> None:
    if not registry.check_token(message.get("token")):
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
    # Ack the new connection FIRST — closing a superseded connection can
    # block for its full close_timeout if that connection is a half-dead
    # zombie (the common case: a node reconnecting after a network blip,
    # before the old socket's own ping/pong has noticed it's gone). The
    # new node's registration must not wait behind that cleanup.
    await websocket.send(json.dumps({"type": "registered"}))
    if superseded is not None:
        _close_in_background(superseded.websocket)

    try:
        async for _message in websocket:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        registry.unregister(node_id, websocket)
```

with:

```python
async def _handle_registration(websocket, registry: NodeRegistry, message: dict) -> None:
    if not registry.check_token(message.get("token")):
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
    # Captured once, right now — never re-fetched from the registry later.
    # If this node reconnects again before this connection's cleanup runs,
    # a fresh registry.get(node_id) at that point would return the *newer*
    # connection's Node, not this one. See the design doc for issue #10.
    node = registry.get(node_id)
    # Ack the new connection FIRST — closing a superseded connection can
    # block for its full close_timeout if that connection is a half-dead
    # zombie (the common case: a node reconnecting after a network blip,
    # before the old socket's own ping/pong has noticed it's gone). The
    # new node's registration must not wait behind that cleanup.
    await websocket.send(json.dumps({"type": "registered"}))
    if superseded is not None:
        _close_in_background(superseded.websocket)

    try:
        async for raw in websocket:
            _dispatch_node_message(node, raw)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        registry.unregister(node_id, websocket)
        # Anything still waiting on this connection (issue #10) needs to
        # fail now, not sit out the full route_request timeout for a node
        # that's already visibly gone — whether cleanly closed, silently
        # timed out via ping/pong (#9), or superseded by a reconnect
        # (_close_in_background above, which triggers this same cleanup
        # for the *old* connection's own _handle_registration task).
        for pending_future in node.pending.values():
            if not pending_future.done():
                pending_future.set_exception(
                    router.NodeDisconnectedError(f"node {node_id!r} disconnected mid-request")
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_server.py -v`
Expected: all PASS, including the new ones from Step 1 and every pre-existing test in the file.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: coordinator routes client completion requests to a registered node"
```

---

### Task 4: `node/request_handler.py` — handle routed requests on the node side

**Files:**
- Create: `src/mycelium/node/request_handler.py`
- Create: `tests/node/test_request_handler.py`

**Interfaces:**
- Consumes: `mycelium.node.vllm_process.VLLMProcess.complete(prompt: str) -> str` (existing, from #7).
- Produces: `handle_messages(websocket, process: VLLMProcess) -> None` (async — Task 5's `cli.py` consumes this).

- [ ] **Step 1: Write the failing tests**

Create `tests/node/test_request_handler.py`:

```python
"""Tests for mycelium.node.request_handler."""

import asyncio
import json

from mycelium.node.request_handler import handle_messages


class _FakeProcess:
    """Stand-in for VLLMProcess — records the prompt it was called with and
    either returns a canned completion or raises."""

    def __init__(self, result=None, error=None):
        self.calls: list[str] = []
        self._result = result
        self._error = error

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeWebsocket:
    """Stand-in for the node's coordinator connection: replays a fixed
    list of incoming raw messages, then blocks — like a real open
    connection sitting idle — until the test calls close_from_test(), at
    which point iteration ends the same way the real `websockets` library
    ends it on a clean close (see websockets.asyncio.connection.Connection
    .__aiter__, which catches ConnectionClosedOK internally and returns):
    no exception propagates out of `async for` in handle_messages.

    Blocking until an explicit close (rather than ending as soon as the
    list is exhausted) matters here: handle_messages spawns a task per
    message and does not await it inline, so the test needs a real chance
    for the event loop to actually run those tasks — which only happens
    while this fake is suspended on `_closed.wait()` — before asserting
    on their effects."""

    def __init__(self, incoming: list[str]):
        self._incoming = list(incoming)
        self.sent: list[str] = []
        self._closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._incoming:
            return self._incoming.pop(0)
        await self._closed.wait()
        raise StopAsyncIteration

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    def close_from_test(self) -> None:
        self._closed.set()


async def test_handle_messages_replies_with_completion_on_success():
    process = _FakeProcess(result="the answer")
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "abc", "prompt": "what's up?"})
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)  # let the spawned per-message task finish and reply
    websocket.close_from_test()
    await handler_task

    assert process.calls == ["what's up?"]
    assert len(websocket.sent) == 1
    reply = json.loads(websocket.sent[0])
    assert reply == {"type": "complete_result", "request_id": "abc", "text": "the answer"}


async def test_handle_messages_replies_with_error_when_complete_raises():
    process = _FakeProcess(error=RuntimeError("vLLM exploded"))
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "abc", "prompt": "hi"})
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    reply = json.loads(websocket.sent[0])
    assert reply == {"type": "complete_error", "request_id": "abc", "reason": "vLLM exploded"}


async def test_handle_messages_ignores_non_complete_messages():
    process = _FakeProcess(result="unused")
    websocket = _FakeWebsocket([json.dumps({"type": "something_else"})])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    assert process.calls == []
    assert websocket.sent == []


async def test_handle_messages_ignores_malformed_json():
    process = _FakeProcess(result="unused")
    websocket = _FakeWebsocket(["not json"])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    assert process.calls == []
    assert websocket.sent == []


async def test_handle_messages_handles_multiple_requests_concurrently():
    process = _FakeProcess(result="answer")
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "1", "prompt": "first"}),
        json.dumps({"type": "complete", "request_id": "2", "prompt": "second"}),
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    assert sorted(process.calls) == ["first", "second"]
    request_ids = {json.loads(raw)["request_id"] for raw in websocket.sent}
    assert request_ids == {"1", "2"}


async def test_handle_messages_returns_normally_on_clean_close():
    """handle_messages must not raise when the connection closes cleanly
    — cli.py's caller distinguishes "closed, reconnect" from a real
    exception via this."""
    process = _FakeProcess(result="unused")
    websocket = _FakeWebsocket([])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.02)
    websocket.close_from_test()

    await handler_task  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_request_handler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.node.request_handler'`.

- [ ] **Step 3: Implement**

Create `src/mycelium/node/request_handler.py`:

```python
"""Handles inbound messages on a node's already-registered coordinator
connection: routed completion requests from the coordinator, forwarded to
the local vLLM process.

See the design doc for issue #10. registration.py owns exactly the
registration handshake; this module owns everything that comes after —
concurrent "complete" requests, each run in its own task so the node can
answer more than one at a time (vLLM does its own internal batching) and
so the event loop stays free to keep answering coordinator pings while a
completion is in flight in a thread (see vllm_process.py's
COMPLETE_TIMEOUT_SECONDS — a single completion can take up to 120s).
"""

from __future__ import annotations

import asyncio
import json

import websockets

from mycelium.node.vllm_process import VLLMProcess


async def handle_messages(websocket, process: VLLMProcess) -> None:
    """Loop reading messages until the connection closes, dispatching each
    "complete" message to its own task. Returns normally when the
    connection closes cleanly, or lets ConnectionClosed propagate on an
    abnormal close — cli.py's caller already treats both the same way
    (reconnect). Any tasks still running when the connection closes are
    cancelled rather than left to send a reply nobody can receive."""
    tasks: set[asyncio.Task] = set()
    try:
        async for raw in websocket:
            task = asyncio.create_task(_handle_complete(websocket, process, raw))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()


async def _handle_complete(websocket, process: VLLMProcess, raw: str) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict) or message.get("type") != "complete":
        return

    request_id = message.get("request_id")
    prompt = message.get("prompt")
    try:
        # Broad except is deliberate here, not sloppy: whatever goes wrong
        # calling vLLM (HTTP error, timeout, malformed response) becomes a
        # complete_error the coordinator/client can see, per the design
        # doc for issue #10 — never left to hang or crash this task.
        text = await asyncio.to_thread(process.complete, prompt)
    except Exception as exc:
        reply = {"type": "complete_error", "request_id": request_id, "reason": str(exc)}
    else:
        reply = {"type": "complete_result", "request_id": request_id, "text": text}

    try:
        await websocket.send(json.dumps(reply))
    except websockets.exceptions.ConnectionClosed:
        pass  # coordinator connection is gone; nothing left to report to
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_request_handler.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/node/request_handler.py tests/node/test_request_handler.py
git commit -m "feat: node request_handler forwards routed completion requests to VLLMProcess"
```

---

### Task 5: Wire `request_handler` into `node/cli.py`

**Files:**
- Modify: `src/mycelium/node/cli.py`
- Modify: `tests/node/test_cli.py`

**Interfaces:**
- Consumes: `mycelium.node.request_handler.handle_messages` (Task 4).
- Produces: nothing new consumed elsewhere — this task changes `cli.py`'s runtime behavior only.

- [ ] **Step 1: Write the failing test**

Add to `tests/node/test_cli.py`, after `test_run_registers_with_coordinator_using_token_and_node_id`:

```python
async def test_run_answers_a_routed_complete_request(tmp_path, monkeypatch, fake_vllm_server):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    token_file = tmp_path / "token"
    token_file.write_text("secret-token\n")

    reply_event = asyncio.Event()
    received_reply = {}

    async def fake_coordinator(websocket):
        await websocket.recv()  # registration
        await websocket.send(json.dumps({"type": "registered"}))
        await websocket.send(json.dumps(
            {"type": "complete", "request_id": "req-1", "prompt": "what is the answer?"}
        ))
        received_reply.update(json.loads(await websocket.recv()))
        reply_event.set()
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
        await asyncio.wait_for(reply_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert received_reply == {
        "type": "complete_result", "request_id": "req-1", "text": "fake completion",
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/node/test_cli.py::test_run_answers_a_routed_complete_request -v`
Expected: FAIL — timeout waiting on `reply_event` (the node never replies, because `_run` currently does `await websocket.wait_closed()` after registering, ignoring everything the coordinator sends).

- [ ] **Step 3: Implement**

In `src/mycelium/node/cli.py`, replace:

```python
from mycelium.node import connection, registration
```

with:

```python
from mycelium.node import connection, registration, request_handler
```

Then replace:

```python
            try:
                await websocket.wait_closed()
                print("connection to coordinator closed, reconnecting...", flush=True)
            except Exception:
                continue
```

with:

```python
            try:
                await request_handler.handle_messages(websocket, process)
                print("connection to coordinator closed, reconnecting...", flush=True)
            except Exception:
                continue
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/node/test_cli.py::test_run_answers_a_routed_complete_request -v`
Expected: PASS.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/node/cli.py tests/node/test_cli.py
git commit -m "feat: node agent answers routed completion requests from the coordinator"
```

---

### Task 6: `client/cli.py` — the one-shot `mycelium-client` command

**Files:**
- Modify: `src/mycelium/client/cli.py`
- Create: `tests/client/__init__.py`
- Create: `tests/client/test_cli.py`

**Interfaces:**
- Consumes: `mycelium.node.connection.build_ssl_context` (existing, from #5 — the same TLS-pinning helper `status_cli.py` already reuses).
- Produces: `parse_args(argv) -> argparse.Namespace`, `complete(coordinator_url, coordinator_cert, token, model, prompt, timeout=CLIENT_COMPLETE_TIMEOUT_SECONDS) -> str` (async), `CompletionError` — Task 7's integration test may reuse `complete()` directly instead of shelling out.

- [ ] **Step 1: Write the failing tests**

Create `tests/client/__init__.py` (empty file, matching `tests/coordinator/__init__.py` and `tests/node/__init__.py`).

Create `tests/client/test_cli.py`:

```python
"""Tests for mycelium.client.cli."""

import asyncio
import json
import ssl

import pytest
import websockets

from mycelium.coordinator import certs, server
from mycelium.client.cli import CompletionError, complete, parse_args


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


def test_parse_args_requires_all_flags():
    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args(["--model", "m", "--prompt", "hi"])


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
            "--model", "Qwen/Qwen2.5-7B-Instruct",
            "--prompt", "hello",
        ]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == str(cert_path)
    assert str(args.token_file) == str(token_file)
    assert args.model == "Qwen/Qwen2.5-7B-Instruct"
    assert args.prompt == "hello"


async def test_complete_returns_text_on_success(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_ws.recv()

            async def fake_node():
                raw = await node_ws.recv()
                msg = json.loads(raw)
                await node_ws.send(json.dumps({
                    "type": "complete_result",
                    "request_id": msg["request_id"],
                    "text": f"echo: {msg['prompt']}",
                }))

            node_task = asyncio.create_task(fake_node())

            text = await complete(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "m", "hello"
            )
            await node_task

    assert text == "echo: hello"


async def test_complete_raises_on_error_reply(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(CompletionError, match="no healthy node"):
            await complete(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "no-such-model", "hi"
            )


async def test_complete_raises_on_wrong_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(CompletionError):
            await complete(f"wss://127.0.0.1:{port}", cert_path, "wrong-token", "m", "hi")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/client/test_cli.py -v`
Expected: FAIL — `ImportError: cannot import name 'CompletionError' from 'mycelium.client.cli'` (the module currently exports only `main`).

- [ ] **Step 3: Implement**

Replace the full contents of `src/mycelium/client/cli.py`:

```python
"""CLI entry point for the Mycelium client.

See the design doc for issue #10. A one-shot request: connect to the
coordinator like mycelium-coordinator-status does, send exactly one
"complete" message, print the result (or a clear error), exit — matching
the phase-0 doc's "a basic client interface to send a prompt and get a
completion back."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

from mycelium.node.connection import build_ssl_context

# 10s past coordinator/router.py's own NODE_COMPLETE_TIMEOUT_SECONDS
# (130s), so the coordinator's timeout fires first and this client gets
# that specific complete_error reason, rather than giving up first with a
# vaguer "coordinator did not respond" message of its own.
CLIENT_COMPLETE_TIMEOUT_SECONDS = 140.0


class CompletionError(Exception):
    """Raised when the coordinator rejects the request, the routed node
    fails or is unavailable, or no response arrives in time."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-client")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    return parser.parse_args(argv)


async def complete(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    model: str,
    prompt: str,
    timeout: float = CLIENT_COMPLETE_TIMEOUT_SECONDS,
) -> str:
    """Send one prompt to the coordinator and return the completion text.

    Raises CompletionError if the coordinator rejects the request, the
    routed node fails or is unavailable, or no response arrives in time.
    """
    ssl_context = build_ssl_context(coordinator_cert)
    async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
        await websocket.send(json.dumps(
            {"type": "complete", "token": token, "model": model, "prompt": prompt}
        ))
        try:
            async with asyncio.timeout(timeout):
                raw = await websocket.recv()
        except TimeoutError:
            raise CompletionError(f"coordinator did not respond within {timeout}s") from None
        except websockets.exceptions.ConnectionClosed:
            raise CompletionError(
                "coordinator closed the connection without responding (check --token-file)"
            ) from None

        message = json.loads(raw)
        if message.get("type") == "complete_result":
            return message["text"]
        if message.get("type") == "complete_error":
            raise CompletionError(message.get("reason", "unknown reason"))
        raise CompletionError(f"unexpected response from coordinator: {message!r}")


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    try:
        text = asyncio.run(
            complete(args.coordinator_url, args.coordinator_cert, token, args.model, args.prompt)
        )
    except CompletionError as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    print(text, flush=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/client/test_cli.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/client/cli.py tests/client/__init__.py tests/client/test_cli.py
git commit -m "feat: mycelium-client sends one prompt to the coordinator and prints the completion"
```

---

### Task 7: Full simulated round-trip integration test

**Files:**
- Modify: `tests/test_integration.py`

**Interfaces:**
- Consumes: `mycelium.client.cli.complete` (Task 6), `mycelium.node.request_handler.handle_messages` (Task 4), `mycelium.node.connection.connect`/`mycelium.node.registration.register` (existing), `mycelium.node.vllm_process.VLLMProcess` (existing).
- Produces: nothing new consumed elsewhere — this is the capstone simulated test proving the whole chain, matching acceptance criterion 1 ("a client can send a prompt to the coordinator and receive a correct completion... with exactly one healthy node registered, every request routes to it correctly") without real hardware. Live-hardware verification is Task 9.

- [ ] **Step 1: Write the failing test**

At the top of `tests/test_integration.py`, add these imports alongside the existing ones:

```python
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from mycelium.client.cli import complete as client_complete
from mycelium.node import request_handler
from mycelium.node.vllm_process import VLLMProcess
```

Then add this class and test to the end of `tests/test_integration.py`:

```python
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
            body = json.loads(self.rfile.read(length))
            prompt = body["messages"][0]["content"]
            reply = json.dumps(
                {"choices": [{"message": {"content": f"real completion for: {prompt}"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


async def test_full_round_trip_client_through_coordinator_to_node_and_back(tmp_path):
    """Simulated end-to-end proof of Phase 0 success criterion #2: a real
    client, a real coordinator, a real node registration+dispatch loop,
    and a real (fake-vLLM-backed) VLLMProcess, all wired together as they
    would be in production — only the vLLM HTTP server itself is faked,
    matching #7/#8's existing test pattern for exercising VLLMProcess
    without real GPU hardware. Live-hardware verification (Task 9) is what
    proves the real vLLM piece; this test proves the wiring."""
    fake_vllm = HTTPServer(("127.0.0.1", 0), _FakeVLLMHandler)
    vllm_thread = Thread(target=fake_vllm.serve_forever, daemon=True)
    vllm_thread.start()
    try:
        process = VLLMProcess(
            model="m", gpu="0", port=fake_vllm.server_address[1]
        )

        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        certs.ensure_cert(cert_path, key_path, "127.0.0.1")

        async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
            port = coordinator.sockets[0].getsockname()[1]

            async def node_loop():
                async for websocket in connection.connect(f"wss://127.0.0.1:{port}", cert_path):
                    await registration.register(
                        websocket, token="secret-token", model="m", node_id="node-a"
                    )
                    await request_handler.handle_messages(websocket, process)

            node_task = asyncio.create_task(node_loop())
            await asyncio.sleep(0.3)  # let the node connect and register

            text = await client_complete(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "m", "what's the capital?"
            )

            node_task.cancel()
            try:
                await node_task
            except asyncio.CancelledError:
                pass

    finally:
        fake_vllm.shutdown()
        vllm_thread.join()

    assert text == "real completion for: what's the capital?"
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_integration.py::test_full_round_trip_client_through_coordinator_to_node_and_back -v`

**TDD deviation, stated explicitly so it isn't mistaken for a mistake mid-execution** (matching the pattern #9's plan used for its own regression test): this test is expected to **PASS immediately**, with no new production code — Tasks 1–6 already implemented every piece it exercises. This test proves the pieces are wired together correctly end to end, it doesn't add new behavior. If it fails or hangs, that's a real integration bug between pieces that each passed their own unit tests in isolation — stop and use `superpowers:systematic-debugging` rather than patching the test to fit.

- [ ] **Step 3: Run the full integration test file**

Run: `pytest tests/test_integration.py -v`
Expected: PASS — all tests in the file, including this new one and the three pre-existing ones (`test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop`, `test_server_and_connection_agree_on_keepalive_settings`, `test_server_and_registration_agree_on_timeout_settings`).

- [ ] **Step 4: Run the full test suite**

Run: `pytest -v`
Expected: PASS, every test in the repo.

- [ ] **Step 5: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: full simulated round trip proving client -> coordinator -> node -> vLLM -> response"
```

---

### Task 8: Update `docs/phases/phase-0-foundation.md`

**Files:**
- Modify: `docs/phases/phase-0-foundation.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed elsewhere — documentation only.

- [ ] **Step 1: Update the `Related:` line**

Replace:

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md) · [Node heartbeat & liveness tracking](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md)
```

with:

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md) · [Node heartbeat & liveness tracking](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md) · [Coordinator forwards a client request](../superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md)
```

- [ ] **Step 2: Add a new bullet to `## Open risks / unresolved decisions`**

After the `**Node heartbeat/liveness: resolved.**` bullet, add:

```markdown
- **Client request routing: resolved.** The coordinator accepts a client's completion request over the same TLS WebSocket port nodes use (`{"type": "complete", "token", "model", "prompt"}`), authenticated with the same shared token, picks the first registered node hosting the requested model, and forwards the request over that node's already-open connection, correlated by a per-request `request_id`. The node runs it through the local `VLLMProcess.complete()` built in #7 and replies; the coordinator relays the result (or a clear error — no healthy node, the node timed out, the node disconnected mid-request, or the node itself reported a failure) back to the client unchanged. See [the design doc](../superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md) for the full decision record.
```

- [ ] **Step 3: Update the `## Next step` section**

Replace:

```markdown
## Next step

Once this document and the risks above are settled, write a Phase 0 implementation plan (not yet started).
```

with:

```markdown
## Next step

Phase 0's core happy path (client → coordinator → node → vLLM → response) is implemented and live-verified as of issue #10 — see success criterion #2 above. #9 already covers success criterion #3 (killing a node removes it from routing). Issues #11 ("Coordinator re-routes when the active node goes down") and #12 ("Clean, immediate failure when no healthy node is available") remain open and refine adjacent behavior further; #10 itself already fails a request immediately with a clear error when `find_node_for_model` finds no match (success criterion #4's basic case), so #12's scope should be checked against that before assuming it's starting from nothing.
```

- [ ] **Step 4: Commit**

```bash
git add docs/phases/phase-0-foundation.md
git commit -m "docs: resolve issue #10's open risk in the phase-0 foundation doc"
```

---

### Task 9: Live-hardware verification

**Files:** none (verification only — the design doc gets a `## Live verification` section appended afterward, see Step 3 below).

**Interfaces:** none — this task exercises the already-implemented, already-tested system as a real operator would.

- [ ] **Step 1: Start a real coordinator and a real node**

On the coordinator host (or localhost, matching #8's live-verification setup):

```bash
openssl rand -hex 32 > token.txt
mycelium-coordinator --token-file token.txt --cert-san-ip <coordinator-ip>
```

On the node host (real GPU hardware — e.g. `a6000`, matching #7/#8's live-verification pattern):

```bash
mycelium-node --coordinator-url wss://<coordinator-ip>:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt \
    --node-id a6000-live-verify --gpu <idle-gpu-index>
```

Confirm registration succeeded, either from the node's own log line (`registered with coordinator as 'a6000-live-verify'`) or via `mycelium-coordinator-status`.

- [ ] **Step 2: Send a real prompt through `mycelium-client`**

```bash
mycelium-client --coordinator-url wss://<coordinator-ip>:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt \
    --model Qwen/Qwen2.5-7B-Instruct --prompt "What is the capital of France?"
```

Expected: a real, correct completion is printed (e.g. mentioning Paris) — proving Phase 0 success criterion #2 for real, not just in simulated tests.

Also confirm the acceptance-criteria failure path: stop the node (`SIGTERM`, same as #7/#8's clean-shutdown verification), wait for it to drop from the registry (`mycelium-coordinator-status` should show no nodes, or wait out the ~50s #9 liveness window if the kill wasn't clean), then re-run the same `mycelium-client` command and confirm it prints a clear `error: no healthy node for model '...'` and exits non-zero, rather than hanging.

- [ ] **Step 3: Record the verification in the design doc**

Append to `docs/superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md`, after the `## Explicitly out of scope for this issue` section, a `## Live verification` section — following the exact narrative style of issue #8's design doc's own `## Live verification` section (real commands run, real output observed, which acceptance criteria each observation closes). Fill in the actual hardware used, actual commands run, and actual output observed during Steps 1–2 above — do not write placeholder/hypothetical output.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md
git commit -m "docs: live-hardware verification for issue #10"
```
