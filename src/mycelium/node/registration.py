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

import websockets

REGISTRATION_TIMEOUT_SECONDS = 10.0


class RegistrationError(Exception):
    """Base class for registration failures (rejected, timed out, or the
    connection closed before a response arrived)."""


class RegistrationRejected(RegistrationError):
    """Raised when the coordinator rejects the registration, responds with
    something other than a clear success, or closes the connection before
    responding at all."""


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
    the coordinator rejects the token (or closes the connection before
    responding), or RegistrationTimeout if no response arrives in time."""
    await websocket.send(
        json.dumps({"type": "register", "token": token, "model": model, "node_id": node_id})
    )
    try:
        # asyncio.timeout(), not asyncio.wait_for(): wait_for has a known
        # race on this Python version where a Task.cancel() landing at the
        # same instant the wrapped awaitable completes can be silently
        # swallowed, leaking the cancellation and hanging the caller's next
        # await forever. asyncio.timeout() doesn't have that failure mode.
        async with asyncio.timeout(timeout):
            raw = await websocket.recv()
    except TimeoutError:
        raise RegistrationTimeout(f"coordinator did not respond within {timeout}s") from None
    except websockets.exceptions.ConnectionClosed as exc:
        raise RegistrationRejected(
            f"coordinator closed the connection during registration: {exc}"
        ) from exc

    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        raise RegistrationRejected("coordinator sent a malformed response")

    if not isinstance(message, dict):
        raise RegistrationRejected(
            f"coordinator sent a non-dict response: {message!r}"
        )

    if message.get("type") == "registered":
        return
    if message.get("type") == "registration_rejected":
        raise RegistrationRejected(message.get("reason", "unknown reason"))
    raise RegistrationRejected(f"unexpected response from coordinator: {message!r}")
