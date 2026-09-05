"""Tests for examples/draft_then_critique.py.

The example lives outside the installed package deliberately (see the
design doc for issue #60), so it is loaded by path rather than imported.
It is tested anyway because it is a consumer of the Flow API: on the #55
branch a public signature changed and a caller outside every task's slice
was missed, shipping a broken operator command. An untested example rots
the same way, and the most expensive place to discover that is #61's
live-hardware session.

The fake-coordinator helpers are duplicated from tests/client/test_flow.py
rather than shared, matching this repo's existing precedent for test
helpers (_client_ssl_context and _fake_identity_verifier are each copied
across three test files).
"""

import importlib.util
import json
from pathlib import Path

import pytest
import websockets

from mycelium.coordinator import certs, server

MODULE_PATH = Path(__file__).parents[2] / "examples" / "draft_then_critique.py"


def _load_example():
    """Load the example by path. examples/ is not an installed package and
    must not become one, so there is nothing to import."""
    spec = importlib.util.spec_from_file_location("draft_then_critique", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCoordinator:
    """Records the raw frames it received and replies however the test
    says — so a test can assert exactly what each hop put on the wire."""

    def __init__(self, reply_for):
        self.received: list[dict] = []
        self._reply_for = reply_for

    async def handler(self, websocket):
        message = json.loads(await websocket.recv())
        self.received.append(message)
        await websocket.send(json.dumps(await self._reply_for(message)))
        await websocket.close()


def _queued(replies):
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


async def test_the_flow_calls_both_models_in_order(tmp_path):
    agent = _load_example()
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a better draft")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = await agent.run(
            url, cert_path, "secret-token",
            draft_model="small-model", critique_model="big-model",
            task="Why is the sky blue?",
        )
    finally:
        running.close()
        await running.wait_closed()

    assert [hop.model for hop in flow.hops] == ["small-model", "big-model"]
    assert flow.hops[1].text == "a better draft"


async def test_the_critique_hop_carries_exactly_what_it_named(tmp_path):
    """The load-bearing assertion: exact equality, not 'contains the
    draft'. A containment check would pass against an implementation that
    also resent the first hop's messages."""
    agent = _load_example()
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a better draft")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        await agent.run(
            url, cert_path, "secret-token",
            draft_model="small-model", critique_model="big-model",
            task="Why is the sky blue?",
        )
    finally:
        running.close()
        await running.wait_closed()

    assert fake.received[0]["messages"] == [
        {"role": "user", "content": "Why is the sky blue?"}
    ]
    assert fake.received[1]["messages"] == [
        {"role": "user", "content": "Improve this answer to: Why is the sky blue?"},
        {"role": "assistant", "content": "a draft"},
    ]


async def test_the_models_are_the_ones_asked_for(tmp_path):
    agent = _load_example()
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a better draft")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        await agent.run(
            url, cert_path, "secret-token",
            draft_model="tiny", critique_model="huge",
            task="anything",
        )
    finally:
        running.close()
        await running.wait_closed()

    assert [frame["model"] for frame in fake.received] == ["tiny", "huge"]


async def test_a_failed_hop_propagates_as_a_hop_error(tmp_path):
    """run() does not swallow failures — main() decides what to do with
    them, so the flow function stays reusable."""
    from mycelium.client import HopError

    agent = _load_example()
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error", "reason": "context too long",
        "fault": "client", "exposed": [], "elapsed_ms": 3,
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        with pytest.raises(HopError) as exc:
            await agent.run(
                url, cert_path, "secret-token",
                draft_model="small-model", critique_model="big-model",
                task="anything",
            )
    finally:
        running.close()
        await running.wait_closed()

    assert exc.value.fault == "client"


def test_the_defaults_name_two_different_models():
    agent = _load_example()

    assert agent.DEFAULT_DRAFT_MODEL != agent.DEFAULT_CRITIQUE_MODEL
    assert agent.DEFAULT_TASK
