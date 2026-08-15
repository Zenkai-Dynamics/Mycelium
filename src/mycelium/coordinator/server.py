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

# These three also double as #9's node-liveness mechanism: a silent node
# (no pong within PING_TIMEOUT_SECONDS of a ping) has the library start a
# close handshake it can never complete, so its connection is force-closed
# after CLOSE_TIMEOUT_SECONDS more — which _handle_registration's cleanup
# already turns into a registry drop. See
# test_silently_unresponsive_node_is_dropped_within_ping_timeout_window in
# tests/coordinator/test_server.py. Worst case from "node goes silent" to
# "dropped from the registry": PING_INTERVAL_SECONDS + PING_TIMEOUT_SECONDS
# + CLOSE_TIMEOUT_SECONDS ≈ 50s.
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20
CLOSE_TIMEOUT_SECONDS = 10
FIRST_MESSAGE_TIMEOUT_SECONDS = 10.0

# Fire-and-forget cleanup tasks (closing a superseded connection) must keep
# a reference somewhere, or asyncio may garbage-collect them mid-execution.
# This module-level set is that reference; each task removes itself when done.
_background_tasks: set[asyncio.Task] = set()


def _close_in_background(websocket) -> None:
    task = asyncio.create_task(websocket.close())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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
        try:
            await websocket.send(json.dumps(
                {"type": "complete_error", "reason": "model and prompt are required"}
            ))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.close()
        return

    try:
        node = registry.find_node_for_model(model)
        if node is None:
            raise router.NoHealthyNodeError(f"no healthy node for model {model!r}")
        text = await router.route_request(node, prompt)
    except router.RoutingError as exc:
        try:
            await websocket.send(json.dumps({"type": "complete_error", "reason": str(exc)}))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.close()
        return

    try:
        await websocket.send(json.dumps({"type": "complete_result", "text": text}))
    except websockets.exceptions.ConnectionClosed:
        return
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


async def _handle_status_query(websocket, registry: NodeRegistry, message: dict) -> None:
    if not registry.check_token(message.get("token")):
        await websocket.close()
        return
    await websocket.send(json.dumps({"type": "status", "nodes": registry.list_nodes()}))
    await websocket.close()


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
        close_timeout=CLOSE_TIMEOUT_SECONDS,
    )
