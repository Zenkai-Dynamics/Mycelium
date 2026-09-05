"""Tests for mycelium.client.flow."""

import json

import pytest
import websockets

from mycelium.client.flow import Flow, HopError, assistant, system, user
from mycelium.coordinator import certs, server


class _FakeCoordinator:
    """A coordinator that records the raw frames it received and replies
    however the test says. Used instead of the real server where a test
    needs to inspect exactly what went on the wire.

    `reply_for` is an async callable taking the received message and
    returning the reply dict, so a test with concurrent calls can key its
    reply off the request rather than off arrival order — each call opens
    its own connection, so a shared queue would race — and can sleep to
    control the order replies come back.
    """

    def __init__(self, reply_for):
        self.received: list[dict] = []
        self._reply_for = reply_for

    async def handler(self, websocket):
        message = json.loads(await websocket.recv())
        self.received.append(message)
        await websocket.send(json.dumps(await self._reply_for(message)))
        await websocket.close()


def _queued(replies):
    """A reply_for that ignores the request and returns each reply in
    order — for tests whose calls are strictly sequential, where arrival
    order is the call order."""
    remaining = list(replies)

    async def reply_for(message):
        return remaining.pop(0)

    return reply_for


def _result(text, node="a3f9c2e1b4d6f8a0", identity="7b1d4408c2e6f1a3"):
    return {
        "type": "complete_result", "text": text,
        "exposed": [{"node_handle": node, "identity_handle": identity}],
        "elapsed_ms": 42,
    }


async def _serve_fake(tmp_path, fake):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    ssl_context = server.build_ssl_context(cert_path, key_path)
    running = await websockets.serve(fake.handler, "127.0.0.1", 0, ssl=ssl_context)
    port = running.sockets[0].getsockname()[1]
    return running, f"wss://127.0.0.1:{port}", cert_path


async def test_only_what_the_call_named_is_sent(tmp_path):
    """THE test for this library: the second hop must carry exactly the
    two messages it named — not the task, not the first hop's input, not
    anything the flow happens to remember. Asserted against the wire,
    because asserting Hop.sent would pass even if call() sent something
    else."""
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a critique")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        draft = await flow.call("small-model", messages=[user("write a haiku about rain")])
        await flow.call(
            "big-model",
            messages=[user("critique this"), assistant(draft.text)],
        )
    finally:
        running.close()
        await running.wait_closed()

    assert fake.received[0]["messages"] == [
        {"role": "user", "content": "write a haiku about rain"}
    ]
    assert fake.received[1]["messages"] == [
        {"role": "user", "content": "critique this"},
        {"role": "assistant", "content": "a draft"},
    ]
    assert "write a haiku about rain" not in json.dumps(fake.received[1]), (
        "the first hop's content must not reach the second unless named"
    )


async def test_the_record_survives_the_caller_mutating_its_list(tmp_path):
    """The record is the evidence behind the exposure report. An agent
    reusing and trimming a message list must not rewrite history."""
    fake = _FakeCoordinator(_queued([_result("ok")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    messages = [user("original")]
    try:
        flow = Flow(url, cert_path, "secret-token")
        hop = await flow.call("m", messages=messages)
    finally:
        running.close()
        await running.wait_closed()

    messages[0]["content"] = "tampered"
    messages.append(user("added later"))

    assert hop.sent == [{"role": "user", "content": "original"}]
    assert flow.hops[0].sent == [{"role": "user", "content": "original"}]


async def test_a_successful_hop_records_everything(tmp_path):
    fake = _FakeCoordinator(_queued([_result("the answer")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        hop = await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    assert hop.index == 0
    assert hop.model == "m"
    assert hop.text == "the answer"
    assert hop.error is None and hop.fault is None
    assert hop.exposed == [
        {"node_handle": "a3f9c2e1b4d6f8a0", "identity_handle": "7b1d4408c2e6f1a3"}
    ]
    assert hop.elapsed_ms == 42
    assert isinstance(hop.wall_ms, int) and hop.wall_ms >= 0
    assert flow.hops == [hop]


async def test_a_failed_hop_is_recorded_then_raises_with_its_fault(tmp_path):
    """#59 requires the record to stay intact and usable after a failure,
    and #58's fault is what lets an agent decide whether to trim its
    context or try a different node."""
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error",
        "reason": "This model's maximum context length is 32768 tokens",
        "fault": "client",
        "exposed": [{"node_handle": "a3f9c2e1b4d6f8a0", "identity_handle": None}],
        "elapsed_ms": 7,
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        with pytest.raises(HopError) as exc:
            await flow.call("m", messages=[user("far too much")])
    finally:
        running.close()
        await running.wait_closed()

    assert exc.value.fault == "client"
    assert "maximum context length" in str(exc.value)
    assert len(flow.hops) == 1
    recorded = flow.hops[0]
    assert recorded.text is None
    assert recorded.fault == "client"
    assert recorded.sent == [{"role": "user", "content": "far too much"}]
    assert exc.value.hop is recorded


async def test_a_transport_failure_is_recorded_as_a_hop(tmp_path):
    """Nothing reached a node, so no exposure and no elapsed_ms — but the
    attempt is still part of the flow's history."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    flow = Flow("wss://127.0.0.1:1", cert_path, "secret-token")
    with pytest.raises(HopError):
        await flow.call("m", messages=[user("hi")])

    assert len(flow.hops) == 1
    assert flow.hops[0].exposed == []
    assert flow.hops[0].elapsed_ms is None
    assert flow.hops[0].error


def test_the_message_helpers_build_plain_dicts():
    assert system("be terse") == {"role": "system", "content": "be terse"}
    assert user("hi") == {"role": "user", "content": "hi"}
    assert assistant("hello") == {"role": "assistant", "content": "hello"}
