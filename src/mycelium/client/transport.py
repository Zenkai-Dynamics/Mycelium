"""One request/response round trip to the coordinator.

Extracted for issue #59, when Flow became the third client-side caller of
the same connect-send-receive-close sequence — after mycelium-client and
the flow library itself. Two hand-rolled copies of this against a wire
that issues #63 and #58 both changed is the drift the extraction exists
to prevent.

Deliberately knows nothing about completions, faults or exposure: it
returns whatever reply arrives, parsed. A `complete_error` is data the
caller interprets, not an exception this module raises — the CLI turns it
into a CompletionError, Flow turns it into a HopError with the fault and
hop attached, and neither has to translate an exception it never wanted.
Only transport-level failures raise here.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import websockets

from mycelium.node.connection import build_ssl_context


class TransportError(Exception):
    """The exchange itself failed: the coordinator could not be reached, no
    reply arrived within the timeout, or the connection closed without a
    response (most often a rejected token).

    Every way the round trip can fail short of producing a reply arrives
    as this one class, so a caller has exactly one thing to catch. In
    particular a refused or unreachable connection raises OSError from
    `websockets.connect`, which would otherwise escape as a bare OSError
    and leave Flow unable to record the attempted hop.
    """


async def request(
    coordinator_url: str, coordinator_cert: Path, message: dict, timeout: float
) -> dict:
    """Send `message` to the coordinator and return its reply, parsed.

    Raises TransportError if the coordinator cannot be reached, no reply
    arrives within `timeout`, or the connection closes first. Any reply
    that does arrive is returned as-is, including a complete_error —
    deciding what a reply means belongs to the caller.
    """
    ssl_context = build_ssl_context(coordinator_cert)
    try:
        async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
            await websocket.send(json.dumps(message))
            try:
                async with asyncio.timeout(timeout):
                    raw = await websocket.recv()
            except TimeoutError:
                raise TransportError(
                    f"coordinator did not respond within {timeout}s"
                ) from None
            except websockets.exceptions.ConnectionClosed:
                raise TransportError(
                    "coordinator closed the connection without responding (check the token)"
                ) from None
    except OSError as exc:
        # Connection establishment failed — refused, unreachable host, DNS
        # failure, TLS handshake rejected. `websockets.connect` is lazy, so
        # these surface when the context manager is entered rather than
        # from the call itself. TransportError is not an OSError, so the
        # two raises above pass through this handler untouched.
        raise TransportError(f"could not reach the coordinator: {exc}") from None
    return json.loads(raw)
