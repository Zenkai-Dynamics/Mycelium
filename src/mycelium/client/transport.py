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

    Every way the round trip can fail short of producing a parsed reply
    arrives as this one class, so a caller has exactly one thing to catch.
    That claim is only worth making if it holds for failures from three
    unrelated hierarchies, which is why `request` catches all three:

    - `OSError` — a refused or unreachable host, a DNS failure, a
      rejected TLS handshake, all raised out of `websockets.connect`.
    - `websockets.exceptions.WebSocketException` — a protocol-level
      failure such as a close during the send, or an endpoint that
      answers the upgrade with plain HTTP. These derive from
      `WebSocketException`, *not* from `OSError`, so catching only
      `OSError` let them escape.
    - `json.JSONDecodeError` — a reply arrived but is not JSON. Parsing
      is part of the round trip this module promises, so a failure to
      parse is a failure of the round trip.

    Anything that escaped instead would leave Flow unable to record the
    attempted hop — the gap this wrapping exists to close — and would
    reach mycelium-client as a traceback, since it catches only
    CompletionError. See the design doc for issue #59.
    """


async def request(
    coordinator_url: str, coordinator_cert: Path, message: dict, timeout: float
) -> dict:
    """Send `message` to the coordinator and return its reply, parsed.

    Raises TransportError if the coordinator cannot be reached, no reply
    arrives within `timeout`, the connection closes or fails first, or
    what comes back is not JSON. Any reply that does parse is returned
    as-is, including a complete_error — deciding what a reply means
    belongs to the caller.
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
        # Parsed inside the try: a reply that is not JSON is a failure of
        # this round trip, and leaving it outside let a JSONDecodeError
        # escape the one class this module promises (issue #59).
        return json.loads(raw)
    # TransportError is none of the three types below — it derives from
    # Exception directly — so the raises inside the block above pass
    # through every handler untouched and keep their specific wording.
    except OSError as exc:
        # Connection establishment failed — refused, unreachable host, DNS
        # failure, TLS handshake rejected. `websockets.connect` is lazy, so
        # these surface when the context manager is entered rather than
        # from the call itself.
        raise TransportError(f"could not reach the coordinator: {exc}") from None
    except websockets.exceptions.WebSocketException as exc:
        # Reached but the websocket exchange itself failed: a close during
        # the send, or an endpoint that answered the upgrade with plain
        # HTTP (the wrong port, or a proxy). These derive from
        # WebSocketException rather than OSError, so the handler above
        # never saw them.
        raise TransportError(f"the connection to the coordinator failed: {exc}") from None
    except json.JSONDecodeError as exc:
        raise TransportError(f"the coordinator's reply was not JSON: {exc}") from None
