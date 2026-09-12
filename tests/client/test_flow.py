"""Tests for mycelium.client.flow."""

import asyncio
import json

import pytest
import websockets

from mycelium.client import transport
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


async def test_the_wire_matches_the_record_when_the_list_is_mutated_in_flight(tmp_path):
    """The regression test for the window between `call` taking its deep
    copy and the request being serialised.

    `call` is async, and its first suspension point is inside
    `transport.request` — `websockets.connect.__aenter__` — which happens
    *before* `json.dumps` turns the request into a frame. If the caller's
    live list were the one placed in the request, any coroutine mutating
    it during that window would change what the volunteer receives while
    the record still showed the pre-mutation copy: the volunteer would
    have seen more than the evidence says, which is the one direction
    ADR-0004 cannot tolerate.

    Mutating *after* the call returns cannot catch this — that is the
    older test above, and it passes either way. This one starts the call
    as a task, yields once so it reaches its first await, and mutates
    while it is in flight. It asserts on the frame the coordinator
    actually received, because asserting only on `hop.sent` would pass
    against exactly the bug it exists to catch.
    """
    fake = _FakeCoordinator(_queued([_result("ok")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    messages = [user("original")]
    try:
        flow = Flow(url, cert_path, "secret-token")
        call = asyncio.create_task(flow.call("m", messages=messages))
        # One yield is enough and is not a race: create_task queues the
        # call's first step ahead of this coroutine's resumption, so the
        # call is already suspended inside connect() when the mutation
        # below runs, and the request cannot be serialised until connect
        # finishes several loop iterations later.
        await asyncio.sleep(0)
        messages[0]["content"] = "tampered"
        messages.append(user("added later"))
        hop = await call
    finally:
        running.close()
        await running.wait_closed()

    assert fake.received[0]["messages"] == [{"role": "user", "content": "original"}], (
        "the volunteer must receive exactly what the record says was sent"
    )
    assert hop.sent == fake.received[0]["messages"]


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


async def test_a_refused_connection_is_recorded_as_a_hop(tmp_path):
    """A refused connection: nothing reached a node, so no exposure and no
    elapsed_ms — but the attempt is still part of the flow's history.

    The empty `exposed` is honest here because the content demonstrably
    never left this machine. It does not generalise to every transport
    failure: a timeout records the same empty list while the coordinator
    may already have handed the content to volunteers, so there it is a
    floor rather than a count. That gap is documented on Hop rather than
    tested, because the client has no way to observe what it never heard.
    """
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


async def test_exposure_groups_by_node_and_by_identity(tmp_path):
    """Two nodes under one identity is the case the report exists for:
    the per-identity cap is three, so three nodes can be one person."""
    fake = _FakeCoordinator(_queued([
        _result("one", node="node-aaaa", identity="ident-1111"),
        _result("two", node="node-bbbb", identity="ident-1111"),
        _result("three", node="node-aaaa", identity="ident-1111"),
    ]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        for _ in range(3):
            await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    exposure = flow.exposure()
    assert exposure.by_node == {"node-aaaa": [0, 2], "node-bbbb": [1]}
    assert exposure.by_identity == {"ident-1111": [0, 1, 2]}, (
        "three hops across two nodes were all seen by one volunteer"
    )


async def test_exposure_keeps_an_unresolved_identity_under_none(tmp_path):
    """A node whose identity could not be resolved still saw the hop —
    dropping it would under-report exposure."""
    fake = _FakeCoordinator(_queued([_result("one", node="node-aaaa", identity=None)]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    assert flow.exposure().by_identity == {None: [0]}


async def test_exposure_counts_a_failed_hop(tmp_path):
    """The node read the conversation before rejecting it."""
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error", "reason": "too long", "fault": "client",
        "exposed": [{"node_handle": "node-aaaa", "identity_handle": "ident-1111"}],
        "elapsed_ms": 5,
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        with pytest.raises(HopError):
            await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    assert flow.exposure().by_node == {"node-aaaa": [0]}


async def test_a_failover_hop_reports_each_node_and_each_identity_once(tmp_path):
    """#63 makes a hop's exposure a list when the coordinator failed over.

    Two of the three nodes here are bound to one identity, which is a
    designed-for state rather than a curiosity: the per-identity cap is
    three nodes, so a failover can land on the same volunteer's second
    machine. The coordinator never repeats a *node* within a hop, so
    `by_node` is safe by construction, but a per-identity list that
    appended unconditionally would report `{"ident-1111": [0, 0]}` — and
    the consumer in docs/OPERATIONS.md would print "saw 2 of 1 hops".
    """
    fake = _FakeCoordinator(_queued([{
        "type": "complete_result", "text": "ok",
        "exposed": [
            {"node_handle": "node-aaaa", "identity_handle": "ident-1111"},
            {"node_handle": "node-bbbb", "identity_handle": "ident-1111"},
            {"node_handle": "node-cccc", "identity_handle": "ident-2222"},
        ],
        "elapsed_ms": 9,
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    exposure = flow.exposure()
    assert exposure.by_node == {"node-aaaa": [0], "node-bbbb": [0], "node-cccc": [0]}
    assert exposure.by_identity == {"ident-1111": [0], "ident-2222": [0]}, (
        "one volunteer saw one hop, however many of their nodes it touched"
    )


async def test_concurrent_calls_are_recorded_in_call_order(tmp_path):
    """Fanning one question out to several models is a plausible agent
    shape; the record must read back in the agent's order, not the order
    the models happened to answer.

    The fake answers slowest for the call made FIRST, so completion order
    is the reverse of call order. A test whose replies came back in call
    order would pass even if hops were appended on completion with no
    index — this one only passes if the index assigned at call time is
    what orders the record.
    """

    async def slow_reversed(message):
        content = message["messages"][0]["content"]
        await asyncio.sleep({"a": 0.15, "b": 0.10, "c": 0.05}[content])
        return _result(f"reply to {content}")

    fake = _FakeCoordinator(slow_reversed)
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await asyncio.gather(
            flow.call("m", messages=[user("a")]),
            flow.call("m", messages=[user("b")]),
            flow.call("m", messages=[user("c")]),
        )
    finally:
        running.close()
        await running.wait_closed()

    assert [hop.index for hop in flow.hops] == [0, 1, 2]
    assert [hop.sent[0]["content"] for hop in flow.hops] == ["a", "b", "c"], (
        "the record must follow call order, not the order replies arrived"
    )


def test_the_public_surface_is_importable_from_the_package():
    from mycelium.client import Exposure, Flow, Hop, HopError, assistant, system, user

    assert all([Exposure, Flow, Hop, HopError, assistant, system, user])


async def test_list_models_returns_what_the_coordinator_reports(tmp_path):
    fake = _FakeCoordinator(_queued([{
        "type": "models",
        "models": [
            {"model": "small-model", "healthy_nodes": 1},
            {"model": "big-model", "healthy_nodes": 2},
        ],
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        models = await flow.list_models()
    finally:
        running.close()
        await running.wait_closed()

    assert models == [
        {"model": "small-model", "healthy_nodes": 1},
        {"model": "big-model", "healthy_nodes": 2},
    ]
    assert fake.received[0] == {"type": "list_models", "token": "secret-token"}


async def test_list_models_does_not_touch_the_flow_record(tmp_path):
    """Discovery is not a hop: nothing was sent to a node, nobody saw
    anything, and there is no Hop to record. See the design doc for #56."""
    fake = _FakeCoordinator(_queued([{"type": "models", "models": []}]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await flow.list_models()
    finally:
        running.close()
        await running.wait_closed()

    assert flow.hops == []
    assert flow.exposure().by_node == {}


async def test_list_models_propagates_a_transport_failure(tmp_path):
    """A discovery failure is not a hop failure, so HopError would be the
    wrong type — there is no Hop to attach and no fault to report."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    flow = Flow("wss://127.0.0.1:1", cert_path, "secret-token")
    with pytest.raises(transport.TransportError):
        await flow.list_models()


async def test_list_models_raises_transport_error_on_a_malformed_models_reply(tmp_path):
    """A reply with the right `type` but a missing `models` key used to
    raise KeyError on `reply["models"]` — a traceback where a wrong-type
    reply already gets a clean TransportError. Both must be treated the
    same way; the coordinator's cert was pinned, but a malformed reply is
    still not a crash the caller should have to handle specially."""
    fake = _FakeCoordinator(_queued([{"type": "models"}]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        with pytest.raises(transport.TransportError):
            await flow.list_models()
    finally:
        running.close()
        await running.wait_closed()


async def test_list_models_interleaved_with_calls_leaves_hop_indices_undisturbed(tmp_path):
    """list_models never references _hops or _next_index, so it is true by
    construction that it cannot disturb the hop-index counter — but the
    flow record is what ADR-0004's exposure claim rests on, so it is worth
    the cheap proof: a list_models() call between two call()s must not
    consume an index or leave a stray record."""
    fake = _FakeCoordinator(_queued([
        _result("first"),
        {"type": "models", "models": []},
        _result("second"),
    ]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await flow.call("m", messages=[user("one")])
        await flow.list_models()
        await flow.call("m", messages=[user("two")])
    finally:
        running.close()
        await running.wait_closed()

    assert [hop.index for hop in flow.hops] == [0, 1]
    assert len(flow.hops) == 2


def test_transport_error_is_exported_from_the_package():
    from mycelium.client import TransportError

    assert TransportError is transport.TransportError
