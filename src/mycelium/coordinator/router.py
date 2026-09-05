"""Routes one client completion request to a specific node's already-open
coordinator connection and returns its response.

See the design doc for issue #10. This module owns exactly one exchange
per call: send {"type": "complete", "request_id", "messages"} on the
node's websocket, wait for a correlated reply. Picking *which* node (or
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
from websockets.protocol import State

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
    were waiting for a reply. Catch this when all you care about is that
    the node is unusable and another one should be tried; catch one of
    its two subclasses below when it matters whether the node actually
    saw the request's content."""


class NodeSendFailedError(NodeDisconnectedError):
    """The connection was already unusable *before* we tried to send — so
    nothing reached the node, and it saw none of the request's content.

    Raised only when the connection's state was demonstrably not OPEN at
    the moment route_request went to send. That precondition is what
    makes the "saw nothing" claim true rather than merely likely: in
    websockets 17.0.1, Connection.send_context (asyncio/connection.py)
    writes no bytes at all down its `protocol.state is not expected_state`
    branch, whereas on an OPEN connection it calls send_data() — handing
    the frame to the transport — and only *then* awaits drain(). A
    ConnectionClosed surfacing from that drain would mean the bytes were
    already on their way. See route_request for how that ambiguous case
    is classified instead.

    Split out of NodeDisconnectedError for issue #63: this module's
    docstring used to say a caller "shouldn't need to (and can't)"
    distinguish a failed send from a mid-flight drop, which was true
    until exposure reporting existed. It is a subclass, so callers that
    only care about "this node is gone, try another" are unaffected.
    """


class NodeDroppedError(NodeDisconnectedError):
    """The connection died at a point where the node may already have
    received the request — so it must be reported as having seen its
    content. Two shapes reach here: the connection died while we were
    awaiting a reply (the node very likely read the request), and a send
    that failed on a connection that was OPEN when we started (the frame
    may already have been handed to the transport before the failure).

    The second is deliberately lumped in with the first: issue #63's
    governing principle is that an ambiguous exposure counts as exposure,
    because under-reporting is a false privacy claim in the flattering
    direction. See the design doc for issue #63."""


class NodeError(RoutingError):
    """Raised when the node itself explicitly reports the completion
    failed (a complete_error reply — e.g. vLLM errored), as opposed to
    timing out or disconnecting."""


class ClientRequestError(RoutingError):
    """The node reported that the CLIENT's request was at fault — most
    often context exceeding the model's window.

    Deliberately NOT a subclass of NodeError: an existing `except
    NodeError` site records a crash against the node, and a client's own
    mistake must never do that. See the design doc for issue #58.

    Raised only when the reply says exactly "client". An absent or
    unrecognized value is a NodeError, so a node cannot dodge reputation
    by sending garbage in place of a valid claim.
    """


async def route_request(
    node: Node, messages: list[dict], timeout: float = NODE_COMPLETE_TIMEOUT_SECONDS
) -> str:
    """Send `messages` to `node` over its already-open connection and
    return its completion text.

    Carries the conversation as an array of {role, content} rather than a
    bare prompt string — see the design doc for issue #55. The coordinator
    has already expanded any `prompt` shorthand by the time this is
    called, so the node wire has exactly one shape.

    Raises NodeDisconnectedError if the connection is or becomes unusable,
    NodeTimeoutError if no reply arrives within `timeout`, or NodeError if
    the node explicitly reports a failure. `node.pending` never retains an
    entry for this request once this function returns or raises.

    NodeDisconnectedError is never raised directly: it always arrives as
    one of its two subclasses, and which one is the difference between a
    node that saw the client's conversation and one that did not.
    NodeSendFailedError means the connection was already not OPEN before
    the send, so nothing left the coordinator; NodeDroppedError means the
    bytes may already have reached the node — it dropped mid-flight, or
    the send itself failed on a connection that was still OPEN when we
    started. Catch NodeDisconnectedError when all you need is "this node
    is unusable, try another"; catch the subclasses when it matters
    whether the node read the request. See the design doc for issue #63.
    """
    request_id = str(uuid.uuid4())
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    node.pending[request_id] = future
    try:
        # Sampled *before* the send, because a failed send closes the
        # connection and the state afterwards is CLOSED either way. This
        # is the only evidence available that distinguishes a send that
        # reached nobody from one that may already have put the client's
        # conversation on the wire: websockets 17.0.1 writes nothing when
        # the connection is not OPEN on entry to send_context, but on an
        # OPEN connection it writes the frame to the transport and only
        # then awaits drain(), so a ConnectionClosed out of that drain
        # comes *after* the bytes were handed to the socket. Ambiguity
        # counts as exposure — see the design doc for issue #63.
        was_open = node.websocket.state is State.OPEN
        try:
            await node.websocket.send(
                json.dumps(
                    {"type": "complete", "request_id": request_id, "messages": messages}
                )
            )
        except websockets.exceptions.ConnectionClosed as exc:
            if was_open:
                raise NodeDroppedError(f"node connection closed mid-send: {exc}") from exc
            raise NodeSendFailedError(f"node connection was already closed: {exc}") from exc

        try:
            async with asyncio.timeout(timeout):
                message = await future
        except TimeoutError:
            # Deliberately anonymous. Every RoutingError message here can
            # end up relayed to the client verbatim as a reply's `reason`,
            # and node_id defaults to the volunteer's socket.gethostname()
            # (node/cli.py). A timeout reply carries exactly one exposed
            # handle, so naming the node would hand the client the
            # handle -> machine-name mapping that this whole feature
            # exists to withhold. See the design doc for issue #63.
            raise NodeTimeoutError(f"node did not respond within {timeout}s") from None
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
    if message.get("fault") == "client":
        raise ClientRequestError(
            message.get("reason", "the model rejected the request with no reason given")
        )
    raise NodeError(message.get("reason", "node reported a failure with no reason given"))
