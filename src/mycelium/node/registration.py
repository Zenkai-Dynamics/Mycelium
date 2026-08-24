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

from __future__ import annotations

import asyncio
import json

import websockets

# Bumped from 10.0 (issue #39): a first-time registration can now
# involve the coordinator making an outbound GitHub API call, bounded at
# github_identity.VERIFY_TIMEOUT_SECONDS (5s) — see the design doc for
# issue #39. Kept equal to server.FIRST_MESSAGE_TIMEOUT_SECONDS by
# convention (see test_server_and_registration_agree_on_timeout_settings
# in tests/test_integration.py).
REGISTRATION_TIMEOUT_SECONDS = 15.0


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
    model: str,
    node_id: str,
    public_key: str,
    signature: str,
    github_token: str | None = None,
    timeout: float = REGISTRATION_TIMEOUT_SECONDS,
) -> None:
    """Send the registration message and wait for the coordinator's
    response. Returns normally on success. github_token is only needed
    on this public key's first-ever registration — omit it (leave as
    None) on every later reconnect; the coordinator ignores it once the
    key is already bound, so it's also harmless to keep passing it. See
    the design doc for issue #39. Raises RegistrationRejected if the
    coordinator rejects the registration (or closes the connection
    before responding), or RegistrationTimeout if no response arrives in
    time."""
    message = {
        "type": "register",
        "model": model,
        "node_id": node_id,
        "public_key": public_key,
        "signature": signature,
    }
    if github_token is not None:
        message["github_token"] = github_token
    await websocket.send(json.dumps(message))
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
