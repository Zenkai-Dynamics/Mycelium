"""Tests for mycelium.node.request_handler."""

import asyncio
import json

from mycelium.node.request_handler import handle_messages


class _FakeProcess:
    """Stand-in for VLLMProcess — records the prompt it was called with and
    either returns a canned completion or raises."""

    def __init__(self, result=None, error=None):
        self.calls: list[str] = []
        self._result = result
        self._error = error

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeWebsocket:
    """Stand-in for the node's coordinator connection: replays a fixed
    list of incoming raw messages, then blocks — like a real open
    connection sitting idle — until the test calls close_from_test(), at
    which point iteration ends the same way the real `websockets` library
    ends it on a clean close (see websockets.asyncio.connection.Connection
    .__aiter__, which catches ConnectionClosedOK internally and returns):
    no exception propagates out of `async for` in handle_messages.

    Blocking until an explicit close (rather than ending as soon as the
    list is exhausted) matters here: handle_messages spawns a task per
    message and does not await it inline, so the test needs a real chance
    for the event loop to actually run those tasks — which only happens
    while this fake is suspended on `_closed.wait()` — before asserting
    on their effects."""

    def __init__(self, incoming: list[str]):
        self._incoming = list(incoming)
        self.sent: list[str] = []
        self._closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._incoming:
            return self._incoming.pop(0)
        await self._closed.wait()
        raise StopAsyncIteration

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    def close_from_test(self) -> None:
        self._closed.set()


async def test_handle_messages_replies_with_completion_on_success():
    process = _FakeProcess(result="the answer")
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "abc", "prompt": "what's up?"})
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)  # let the spawned per-message task finish and reply
    websocket.close_from_test()
    await handler_task

    assert process.calls == ["what's up?"]
    assert len(websocket.sent) == 1
    reply = json.loads(websocket.sent[0])
    assert reply == {"type": "complete_result", "request_id": "abc", "text": "the answer"}


async def test_handle_messages_replies_with_error_when_complete_raises():
    process = _FakeProcess(error=RuntimeError("vLLM exploded"))
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "abc", "prompt": "hi"})
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    reply = json.loads(websocket.sent[0])
    assert reply == {"type": "complete_error", "request_id": "abc", "reason": "vLLM exploded"}


async def test_handle_messages_ignores_non_complete_messages():
    process = _FakeProcess(result="unused")
    websocket = _FakeWebsocket([json.dumps({"type": "something_else"})])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    assert process.calls == []
    assert websocket.sent == []


async def test_handle_messages_ignores_malformed_json():
    process = _FakeProcess(result="unused")
    websocket = _FakeWebsocket(["not json"])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    assert process.calls == []
    assert websocket.sent == []


async def test_handle_messages_handles_multiple_requests_concurrently():
    process = _FakeProcess(result="answer")
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "1", "prompt": "first"}),
        json.dumps({"type": "complete", "request_id": "2", "prompt": "second"}),
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    assert sorted(process.calls) == ["first", "second"]
    request_ids = {json.loads(raw)["request_id"] for raw in websocket.sent}
    assert request_ids == {"1", "2"}


async def test_handle_messages_returns_normally_on_clean_close():
    """handle_messages must not raise when the connection closes cleanly
    — cli.py's caller distinguishes "closed, reconnect" from a real
    exception via this."""
    process = _FakeProcess(result="unused")
    websocket = _FakeWebsocket([])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.02)
    websocket.close_from_test()

    await handler_task  # must not raise
