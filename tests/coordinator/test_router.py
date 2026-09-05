"""Tests for mycelium.coordinator.router."""

import asyncio
import json

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

from mycelium.coordinator import router
from mycelium.coordinator.registry import Node


class _FakeNodeWebsocket:
    """Stand-in for a node's websocket. route_request calls send() on it
    and reads `state` — replies arrive by resolving node.pending
    directly, the same way server.py's real dispatch loop will in Task 3.

    `state` mirrors websockets' own Connection.state, which route_request
    samples before sending to tell a connection that was already unusable
    from one that died mid-send (issue #63).
    """

    def __init__(self, send_raises=None, state=State.OPEN):
        self.sent: list[str] = []
        self._send_raises = send_raises
        self.state = state

    async def send(self, raw: str) -> None:
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append(raw)


def _make_node(websocket=None) -> Node:
    return Node(public_key="pubkey-a", node_id="node-a", model="m", websocket=websocket or _FakeNodeWebsocket())


async def _resolve_after(node: Node, delay: float, message: dict) -> None:
    await asyncio.sleep(delay)
    (request_id,) = node.pending.keys()
    node.pending[request_id].set_result({**message, "request_id": request_id})


async def test_route_request_sends_a_complete_message_with_request_id():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result", "text": "hi"}))

    await router.route_request(
        node, [{"role": "user", "content": "what's up?"}], timeout=2.0
    )

    assert len(node.websocket.sent) == 1
    sent = json.loads(node.websocket.sent[0])
    assert sent["type"] == "complete"
    assert sent["messages"] == [{"role": "user", "content": "what's up?"}]
    assert isinstance(sent["request_id"], str) and sent["request_id"]


async def test_route_request_returns_text_on_success():
    node = _make_node()
    asyncio.create_task(
        _resolve_after(node, 0.05, {"type": "complete_result", "text": "the answer"})
    )

    text = await router.route_request(node, [{"role": "user", "content": "prompt"}], timeout=2.0)

    assert text == "the answer"


async def test_route_request_cleans_up_pending_on_success():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result", "text": "hi"}))

    await router.route_request(node, [{"role": "user", "content": "prompt"}], timeout=2.0)

    assert node.pending == {}


async def test_route_request_raises_node_error_when_node_reports_failure():
    node = _make_node()
    asyncio.create_task(
        _resolve_after(node, 0.05, {"type": "complete_error", "reason": "vLLM exploded"})
    )

    with pytest.raises(router.NodeError, match="vLLM exploded"):
        await router.route_request(node, [{"role": "user", "content": "prompt"}], timeout=2.0)

    assert node.pending == {}


async def test_route_request_raises_timeout_when_no_reply_arrives():
    node = _make_node()

    with pytest.raises(router.NodeTimeoutError):
        await router.route_request(node, [{"role": "user", "content": "prompt"}], timeout=0.1)

    assert node.pending == {}


async def test_route_request_raises_disconnected_when_send_fails():
    websocket = _FakeNodeWebsocket(
        send_raises=websockets.exceptions.ConnectionClosedError(None, None)
    )
    node = _make_node(websocket)

    with pytest.raises(router.NodeDisconnectedError):
        await router.route_request(node, [{"role": "user", "content": "prompt"}], timeout=2.0)

    assert node.pending == {}


async def test_send_failure_on_an_already_closed_connection_raises_node_send_failed():
    """Only a connection that was demonstrably not OPEN before the send
    is a send that reached nobody: websockets' send_context writes
    nothing at all down that branch. See the design doc for issue #63."""
    node = _make_node(
        _FakeNodeWebsocket(send_raises=ConnectionClosedError(None, None), state=State.CLOSED)
    )

    with pytest.raises(router.NodeSendFailedError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_send_failure_on_an_open_connection_is_reported_as_a_drop():
    """websockets 17.0.1's send() hands the frame to the transport and
    only *then* awaits drain(), so a ConnectionClosed raised out of a
    connection that was OPEN when we started may well have put the
    client's conversation on the wire already. Issue #63's design says an
    ambiguous case counts as exposure, so this is a drop, not a failed
    send — under-reporting would be a false privacy claim in the
    flattering direction."""
    node = _make_node(
        _FakeNodeWebsocket(send_raises=ConnectionClosedError(None, None), state=State.OPEN)
    )

    with pytest.raises(router.NodeDroppedError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_send_failure_is_still_a_node_disconnected_error():
    """Both new classes subclass NodeDisconnectedError so the
    coordinator's existing failover branch is untouched."""
    node = _make_node(
        _FakeNodeWebsocket(send_raises=ConnectionClosedError(None, None), state=State.CLOSED)
    )

    with pytest.raises(router.NodeDisconnectedError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_drop_while_awaiting_reply_raises_node_dropped():
    """The distinction that matters for exposure: this node received the
    request, so it must be reported as having seen the content."""
    node = _make_node()

    async def drop_after(delay):
        await asyncio.sleep(delay)
        (request_id,) = node.pending.keys()
        node.pending[request_id].set_exception(router.NodeDroppedError("node dropped"))

    asyncio.create_task(drop_after(0.05))

    with pytest.raises(router.NodeDroppedError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_route_request_propagates_disconnected_error_set_on_future():
    node = _make_node()

    async def fail_soon():
        await asyncio.sleep(0.05)
        (request_id,) = node.pending.keys()
        node.pending[request_id].set_exception(
            router.NodeDisconnectedError("node disconnected mid-request")
        )

    asyncio.create_task(fail_soon())

    with pytest.raises(router.NodeDisconnectedError, match="disconnected mid-request"):
        await router.route_request(node, [{"role": "user", "content": "prompt"}], timeout=2.0)

    assert node.pending == {}


async def test_route_request_raises_node_error_when_complete_result_missing_text():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result"}))

    with pytest.raises(router.NodeError, match="malformed complete_result"):
        await router.route_request(node, [{"role": "user", "content": "prompt"}], timeout=2.0)

    assert node.pending == {}


async def test_route_request_sends_the_messages_array_to_the_node():
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result", "text": "ok"}))

    text = await router.route_request(node, messages, timeout=2.0)

    sent = json.loads(node.websocket.sent[0])
    assert sent["messages"] == messages
    assert "prompt" not in sent, "the node wire carries messages only, as of issue #55"
    assert text == "ok"


def test_all_router_errors_are_routing_errors():
    assert issubclass(router.NoHealthyNodeError, router.RoutingError)
    assert issubclass(router.NodeTimeoutError, router.RoutingError)
    assert issubclass(router.NodeDisconnectedError, router.RoutingError)
    assert issubclass(router.NodeError, router.RoutingError)


async def test_client_fault_reply_raises_client_request_error():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {
        "type": "complete_error", "reason": "context too long", "fault": "client",
    }))

    with pytest.raises(router.ClientRequestError, match="context too long"):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_client_request_error_is_not_a_node_error():
    """It must not be catchable as a NodeError, or an existing handler
    would record a crash for it — the bug issue #58 fixes."""
    assert not issubclass(router.ClientRequestError, router.NodeError)
    assert issubclass(router.ClientRequestError, router.RoutingError)


async def test_an_unrecognized_fault_value_is_a_node_error():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {
        "type": "complete_error", "reason": "boom", "fault": "banana",
    }))

    with pytest.raises(router.NodeError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_a_missing_fault_value_is_a_node_error():
    """An older node build predates the field; its failures must keep
    counting exactly as they do today."""
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {
        "type": "complete_error", "reason": "boom",
    }))

    with pytest.raises(router.NodeError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)
