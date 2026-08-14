"""WebSocket server the coordinator runs to accept dial-out node connections.

See ADR-0002 for why nodes dial out rather than the coordinator dialing in.
Phase 0 scope boundary (see the design doc for issue #5): this module only
holds connections open. It carries no registration/auth/routing logic —
that's issue #8 (registration) and #10 (routing).
"""

from __future__ import annotations

import ssl
from pathlib import Path

import websockets

PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20


async def _handle_node(websocket) -> None:
    """Hold a node's connection open. No business logic yet — see module docstring."""
    try:
        async for _message in websocket:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass


def build_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Build the server-side TLS context from the coordinator's cert/key pair."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def serve(host: str, port: int, cert_path: Path, key_path: Path):
    """Start the coordinator's node-facing WebSocket server.

    Returns whatever `websockets.serve` returns: awaitable to get a `Server`
    instance directly, or usable as `async with serve(...) as server:`.
    """
    ssl_context = build_ssl_context(cert_path, key_path)
    return websockets.serve(
        _handle_node,
        host,
        port,
        ssl=ssl_context,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
    )
