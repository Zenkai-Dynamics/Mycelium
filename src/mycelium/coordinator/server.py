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
    """Read the first message (a registration or a status query) and
    dispatch on it. A registered node's connection is then held open with
    no further business logic yet (that's #9/#10's job); a status query
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
