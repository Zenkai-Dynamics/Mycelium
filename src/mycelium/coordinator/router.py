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
from websockets.exceptions import ConnectionClosed

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
        text = message.get("text")
        if text is None:
            raise NodeError("node sent a malformed complete_result (missing text)")
        return text
    # _dispatch_node_message (server.py) only ever resolves this future
    # with a "complete_result" or "complete_error" message — anything else
    # coming out of `await future` above is a set_exception, not this
    # branch — so this is always a complete_error at this point.
    raise NodeError(message.get("reason", "node reported a failure with no reason given"))
