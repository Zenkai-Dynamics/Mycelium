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

import asyncio
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


def test_parse_args_defaults_to_the_documented_models(tmp_path):
    agent = _load_example()

    args = agent.parse_args([
        "--coordinator-url", "wss://x", "--coordinator-cert", "c.pem",
        "--token-file", "t.txt",
    ])

    assert args.draft_model == agent.DEFAULT_DRAFT_MODEL
    assert args.critique_model == agent.DEFAULT_CRITIQUE_MODEL
    assert args.task == agent.DEFAULT_TASK


def test_parse_args_requires_the_connection_flags():
    agent = _load_example()

    with pytest.raises(SystemExit):
        agent.parse_args([])


async def test_main_prints_what_each_hop_sent(tmp_path, capsys):
    """Surfacing what each hop carried is an acceptance criterion in its
    own right — a reader must be able to see the explicit-context rule in
    practice, not just read about it."""
    agent = _load_example()
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a better draft")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret-token")

    try:
        status = await asyncio.to_thread(agent.main, [
            "--coordinator-url", url,
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--draft-model", "small-model",
            "--critique-model", "big-model",
            "--task", "Why is the sky blue?",
        ])
    finally:
        running.close()
        await running.wait_closed()

    assert status == 0
    out = capsys.readouterr().out
    assert "Why is the sky blue?" in out
    assert "Improve this answer to: Why is the sky blue?" in out
    assert "a draft" in out
    assert "a better draft" in out


async def test_main_reports_a_failed_hop_without_a_traceback(tmp_path, capsys):
    agent = _load_example()
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error",
        "reason": "This model's maximum context length is 32768 tokens",
        "fault": "client", "exposed": [], "elapsed_ms": 3,
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret-token")

    try:
        status = await asyncio.to_thread(agent.main, [
            "--coordinator-url", url,
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ])
    finally:
        running.close()
        await running.wait_closed()

    assert status == 1
    out = capsys.readouterr().out
    assert "maximum context length" in out
    assert "client" in out


async def test_main_says_exposure_is_a_floor_when_contact_was_lost(tmp_path, capsys):
    """fault is None means the client never learned who saw its content —
    so the exposure figures understate it, and saying otherwise would
    overclaim. See the design doc for issue #59."""
    agent = _load_example()
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret-token")
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    # Nothing is listening on this port, so the hop fails in transport.
    status = await asyncio.to_thread(agent.main, [
        "--coordinator-url", "wss://127.0.0.1:1",
        "--coordinator-cert", str(cert_path),
        "--token-file", str(token_file),
    ])

    assert status == 1
    assert "floor" in capsys.readouterr().out
