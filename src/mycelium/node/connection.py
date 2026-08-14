"""Dial-out connection from a node to the coordinator.

See ADR-0002: the node initiates and holds the connection open; the
coordinator never dials into a node. Phase 0 scope boundary (see the design
doc for issue #5): this module only connects and reconnects. It sends no
node identity, token, or registration message — that's issue #8.
"""

from __future__ import annotations

import random
import ssl
from collections.abc import Generator
from pathlib import Path

import websockets

PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20

INITIAL_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
BACKOFF_FACTOR = 2.0
JITTER_FRACTION = 0.2


def reconnect_delays() -> Generator[float, None, None]:
    """Exponential backoff with jitter: 1s initial, x2 each step, capped at 30s, +/-20%."""
    delay = INITIAL_DELAY_SECONDS
    while True:
        jitter = delay * JITTER_FRACTION * (2 * random.random() - 1)
        yield max(0.0, delay + jitter)
        delay = min(delay * BACKOFF_FACTOR, MAX_DELAY_SECONDS)


def build_ssl_context(coordinator_cert_path: Path) -> ssl.SSLContext:
    """Build the client-side TLS context that trusts only the coordinator's pinned cert."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(coordinator_cert_path))
    return context


def connect(
    coordinator_url: str,
    coordinator_cert_path: Path,
    *,
    reconnect_delays_factory=reconnect_delays,
):
    """Dial out to the coordinator, reconnecting automatically on drop.

    Returns a `websockets.connect(...)` reconnecting async iterator:
    `async for websocket in connect(...): ...` yields a new, already-open
    connection each time the previous one closes, after waiting according
    to `reconnect_delays_factory`.
    """
    ssl_context = build_ssl_context(coordinator_cert_path)
    return websockets.connect(
        coordinator_url,
        ssl=ssl_context,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
        reconnect_delays=reconnect_delays_factory,
    )
