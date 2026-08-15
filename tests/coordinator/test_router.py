"""Tests for mycelium.coordinator.router."""

import asyncio
import json

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError

from mycelium.coordinator import router
from mycelium.coordinator.registry import Node


class _FakeNodeWebsocket:
    """Stand-in for a node's websocket. route_request only ever calls
    send() on it — replies arrive by resolving node.pending directly, the
    same way server.py's real dispatch loop will in Task 3."""

    def __init__(self, send_raises=None):
        self.sent: list[str] = []
        self._send_raises = send_raises

    async def send(self, raw: str) -> None:
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append(raw)


def _make_node(websocket=None) -> Node:
    return Node(node_id="node-a", model="m", websocket=websocket or _FakeNodeWebsocket())


async def _resolve_after(node: Node, delay: float, message: dict) -> None:
    await asyncio.sleep(delay)
    (request_id,) = node.pending.keys()
    node.pending[request_id].set_result({**message, "request_id": request_id})


async def test_route_request_sends_a_complete_message_with_request_id():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result", "text": "hi"}))

    await router.route_request(node, "what's up?", timeout=2.0)

    assert len(node.websocket.sent) == 1
    sent = json.loads(node.websocket.sent[0])
    assert sent["type"] == "complete"
    assert sent["prompt"] == "what's up?"
    assert isinstance(sent["request_id"], str) and sent["request_id"]


async def test_route_request_returns_text_on_success():
    node = _make_node()
    asyncio.create_task(
        _resolve_after(node, 0.05, {"type": "complete_result", "text": "the answer"})
    )

    text = await router.route_request(node, "prompt", timeout=2.0)

    assert text == "the answer"


async def test_route_request_cleans_up_pending_on_success():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result", "text": "hi"}))

    await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


async def test_route_request_raises_node_error_when_node_reports_failure():
    node = _make_node()
    asyncio.create_task(
        _resolve_after(node, 0.05, {"type": "complete_error", "reason": "vLLM exploded"})
    )

    with pytest.raises(router.NodeError, match="vLLM exploded"):
        await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


async def test_route_request_raises_timeout_when_no_reply_arrives():
    node = _make_node()

    with pytest.raises(router.NodeTimeoutError):
        await router.route_request(node, "prompt", timeout=0.1)

    assert node.pending == {}


async def test_route_request_raises_disconnected_when_send_fails():
    websocket = _FakeNodeWebsocket(
        send_raises=websockets.exceptions.ConnectionClosedError(None, None)
    )
    node = _make_node(websocket)

    with pytest.raises(router.NodeDisconnectedError):
        await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


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
        await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


async def test_route_request_raises_node_error_when_complete_result_missing_text():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result"}))

    with pytest.raises(router.NodeError, match="malformed complete_result"):
        await router.route_request(node, "prompt", timeout=2.0)

    assert node.pending == {}


def test_all_router_errors_are_routing_errors():
    assert issubclass(router.NoHealthyNodeError, router.RoutingError)
    assert issubclass(router.NodeTimeoutError, router.RoutingError)
    assert issubclass(router.NodeDisconnectedError, router.RoutingError)
    assert issubclass(router.NodeError, router.RoutingError)
