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
