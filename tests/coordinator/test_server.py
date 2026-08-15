"""Tests for mycelium.coordinator.server."""

import asyncio
import json
import ssl
import time

import pytest
import websockets

from mycelium.coordinator import certs, router, server
from mycelium.coordinator.registry import NodeRegistry


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def test_node_can_connect_over_tls(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            assert ws.state.name == "OPEN"


async def test_multiple_nodes_can_connect_simultaneously(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws1:
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws2:
                assert ws1.state.name == "OPEN"
                assert ws2.state.name == "OPEN"


async def test_connection_with_wrong_pinned_cert_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    other_cert_path = tmp_path / "other-cert.pem"
    other_key_path = tmp_path / "other-key.pem"
    certs.ensure_cert(other_cert_path, other_key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        wrong_ctx = _client_ssl_context(other_cert_path)
        try:
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=wrong_ctx):
                assert False, "expected connection to be rejected"
        except ssl.SSLCertVerificationError:
            pass


async def test_server_survives_abnormal_disconnect(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        assert ws.state.name == "OPEN"
        ws.transport.close()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws2:
            assert ws2.state.name == "OPEN"


async def test_valid_registration_is_accepted(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            response = json.loads(await ws.recv())
            assert response == {"type": "registered"}


async def test_registration_with_invalid_token_is_rejected_and_closed(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "register", "token": "wrong", "model": "m", "node_id": "node-a"}
            ))
            response = json.loads(await ws.recv())
            assert response["type"] == "registration_rejected"
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_registration_with_missing_token_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps({"type": "register", "model": "m", "node_id": "node-a"}))
            response = json.loads(await ws.recv())
            assert response["type"] == "registration_rejected"


async def test_registered_node_appears_in_status_query(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps({
                "type": "register",
                "token": "secret-token",
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "node_id": "node-a",
            }))
            await node_ws.recv()  # consume the "registered" ack

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response == {
                    "type": "status",
                    "nodes": [{"node_id": "node-a", "model": "Qwen/Qwen2.5-7B-Instruct"}],
                }


async def test_disconnected_node_is_removed_from_registry(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await node_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
        ))
        await node_ws.recv()
        await node_ws.close()
        await asyncio.sleep(0.2)  # let the coordinator notice the disconnect

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []


async def test_duplicate_node_id_replaces_and_closes_old_connection(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        old_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await old_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "model-a", "node_id": "node-a"}
        ))
        await old_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_ws:
            await new_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "model-b", "node_id": "node-a"}
            ))
            await new_ws.recv()

            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await old_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"] == [{"node_id": "node-a", "model": "model-b"}]


async def test_connection_with_no_message_is_closed_after_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "FIRST_MESSAGE_TIMEOUT_SECONDS", 0.3)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=2.0)


async def test_registration_with_non_dict_json_is_closed_not_crashed(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps([1, 2, 3]))  # valid JSON, not a dict
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()

        # Server must still be accepting new connections afterward.
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws2:
            await ws2.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await ws2.recv())
            assert response == {"type": "status", "nodes": []}


async def test_registration_with_null_token_is_rejected_not_crashed(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "register", "token": None, "model": "m", "node_id": "node-a"}
            ))
            response = json.loads(await ws.recv())
            assert response["type"] == "registration_rejected"


async def test_duplicate_node_id_registration_acks_promptly_even_if_old_connection_is_unresponsive(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        old_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await old_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "model-a", "node_id": "node-a"}
        ))
        await old_ws.recv()
        # Simulate a zombie connection: stop reading, so it can't complete
        # a close handshake promptly when the coordinator later closes it.
        old_ws.transport.pause_reading()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_ws:
            await new_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "model-b", "node_id": "node-a"}
            ))
            start = time.monotonic()
            response = json.loads(await asyncio.wait_for(new_ws.recv(), timeout=3.0))
            elapsed = time.monotonic() - start
            assert response == {"type": "registered"}
            assert elapsed < 2.0, (
                f"ack took {elapsed:.2f}s — must not block on closing a zombie superseded connection"
            )


async def test_silently_unresponsive_node_is_dropped_within_ping_timeout_window(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PING_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(server, "PING_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(server, "CLOSE_TIMEOUT_SECONDS", 0.2)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await node_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
        ))
        await node_ws.recv()  # consume the "registered" ack

        # Confirm the node appears in the registry before the liveness window
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response == {
                "type": "status",
                "nodes": [{"node_id": "node-a", "model": "m"}],
            }

        # Simulate the node going silent (network partition, frozen process):
        # stop processing incoming bytes, so it can never answer a ping with
        # a pong, and can never complete the close handshake the coordinator
        # starts after that — without ever sending a WebSocket close frame
        # itself. This is a different failure mode than
        # test_disconnected_node_is_removed_from_registry (clean close) or
        # test_server_survives_abnormal_disconnect (abrupt transport close)
        # — neither of those goes through the ping/pong timeout path this
        # test targets.
        node_ws.transport.pause_reading()

        # Worst case per the design doc for issue #9: PING_INTERVAL_SECONDS
        # to notice the silence, PING_TIMEOUT_SECONDS for the pong that
        # never arrives, then CLOSE_TIMEOUT_SECONDS waiting for a close
        # handshake the silent peer can never complete.
        await asyncio.sleep(
            server.PING_INTERVAL_SECONDS
            + server.PING_TIMEOUT_SECONDS
            + server.CLOSE_TIMEOUT_SECONDS
            + 1.0
        )

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []


async def _run_fake_node(node_ws, reply_for) -> None:
    """Stand-in for a real node agent: replies to every "complete" message
    it receives using reply_for(message) -> dict (the reply body, minus
    request_id — this helper fills that in)."""
    async for raw in node_ws:
        message = json.loads(raw)
        reply = reply_for(message)
        reply["request_id"] = message["request_id"]
        await node_ws.send(json.dumps(reply))


class _FakeNodeWebsocket:
    """Stand-in for a node's websocket, for testing
    server._handle_complete_request's failover logic in isolation from a
    real network connection — mirrors test_router.py's _FakeNodeWebsocket."""

    def __init__(self, send_raises=None):
        self.sent: list[str] = []
        self._send_raises = send_raises

    async def send(self, raw: str) -> None:
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append(raw)


class _FakeClientWebsocket:
    """Collects what _handle_complete_request sends back to the client,
    without a real network connection."""

    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        self.closed = True


async def test_complete_request_routes_to_registered_node_and_returns_result(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_ws.recv()  # consume "registered"
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_result", "text": f"echo: {msg['prompt']}"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hello"}
                ))
                response = json.loads(await client_ws.recv())
                assert response == {"type": "complete_result", "text": "echo: hello"}

            node_task.cancel()


async def test_complete_request_with_no_matching_node_returns_error(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "no-such-model", "prompt": "hi"}
            ))
            response = json.loads(await client_ws.recv())
            assert response["type"] == "complete_error"
            assert "no-such-model" in response["reason"]


async def test_complete_request_with_wrong_token_is_closed_without_reply(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "wrong", "model": "m", "prompt": "hi"}
            ))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await client_ws.recv()


async def test_complete_request_with_missing_prompt_returns_error(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m"}
            ))
            response = json.loads(await client_ws.recv())
            assert response["type"] == "complete_error"


async def test_complete_request_node_reports_failure_is_relayed_to_client(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_error", "reason": "vLLM exploded"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())
                assert response == {"type": "complete_error", "reason": "vLLM exploded"}

            node_task.cancel()


async def test_complete_request_node_disconnect_mid_request_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await node_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
        ))
        await node_ws.recv()

        async def receive_then_never_reply():
            await node_ws.recv()  # accept the routed "complete", then go silent

        node_task = asyncio.create_task(receive_then_never_reply())

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
            ))
            await node_task  # make sure the node has received the routed request
            await node_ws.close()  # simulate the node dying mid-request

            start = time.monotonic()
            response = json.loads(await asyncio.wait_for(client_ws.recv(), timeout=5.0))
            elapsed = time.monotonic() - start
            assert response["type"] == "complete_error"
            assert elapsed < 3.0, (
                f"client waited {elapsed:.2f}s — a node disconnect mid-request must fail "
                "fast, not wait out the full 30s NODE_COMPLETE_TIMEOUT_SECONDS"
            )


async def test_complete_request_client_disconnect_before_reply_does_not_crash_server(
    tmp_path, caplog
):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await node_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
        ))
        await node_ws.recv()

        async def reply_after_client_gone():
            raw = await node_ws.recv()  # the routed "complete"
            message = json.loads(raw)
            await asyncio.sleep(0.2)  # give the client time to disconnect first
            await node_ws.send(json.dumps(
                {"type": "complete_result", "request_id": message["request_id"], "text": "too late"}
            ))

        node_task = asyncio.create_task(reply_after_client_gone())

        client_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await client_ws.send(json.dumps(
            {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
        ))
        await client_ws.close()  # disconnect before the node replies

        await node_task  # let the coordinator attempt (and fail) its send to the gone client

        # Server must still be accepting new connections afterward — the
        # send failing above must not have crashed the connection handler.
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == [{"node_id": "node-a", "model": "m"}]

        await node_ws.close()

    # The connection handler must have swallowed the send failure quietly,
    # not let it escape and get logged as a "connection handler failed"
    # error by the websockets library (the un-fixed behavior).
    assert not any(record.levelno >= 40 for record in caplog.records), (
        "an error was logged — the ConnectionClosed from sending to the "
        "already-gone client must be caught, not left to propagate"
    )


async def test_superseded_node_connection_fails_only_its_own_pending_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        old_node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await old_node_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
        ))
        await old_node_ws.recv()

        old_client_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)

        async def old_client_request():
            await old_client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "old"}
            ))
            return json.loads(await old_client_ws.recv())

        old_request_task = asyncio.create_task(old_client_request())

        # Confirm the routed "complete" actually reached the OLD connection
        # before proceeding, so we know route_request has registered a
        # pending future on the OLD node's captured Node object.
        routed = json.loads(await old_node_ws.recv())
        assert routed["type"] == "complete"

        # Register a second connection under the same node_id — supersedes
        # the old one (issue #8 behavior); the coordinator closes
        # old_node_ws in the background.
        new_node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await new_node_ws.send(json.dumps(
            {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
        ))
        await new_node_ws.recv()

        # The OLD client's pending request must fail fast via the OLD
        # connection's own disconnect cleanup — not sit out the full 30s
        # NODE_COMPLETE_TIMEOUT_SECONDS.
        start = time.monotonic()
        old_response = await asyncio.wait_for(old_request_task, timeout=5.0)
        elapsed = time.monotonic() - start
        assert old_response["type"] == "complete_error"
        assert elapsed < 3.0, (
            f"old client waited {elapsed:.2f}s — a superseded connection's cleanup must "
            "fail its own pending requests fast, not wait out the full 30s timeout"
        )

        # The NEW connection's own pending dict must be untouched by the
        # old connection's cleanup: route a fresh request through it and
        # confirm it gets a correct, normal reply.
        node_task = asyncio.create_task(_run_fake_node(
            new_node_ws, lambda msg: {"type": "complete_result", "text": f"echo: {msg['prompt']}"}
        ))
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_client_ws:
            await new_client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "new"}
            ))
            new_response = json.loads(await new_client_ws.recv())
            assert new_response == {"type": "complete_result", "text": "echo: new"}

        node_task.cancel()
        await old_client_ws.close()


async def test_concurrent_complete_requests_to_same_node_get_correct_replies(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await node_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_ws.recv()

            async def flaky_reversing_node():
                # Reply out of arrival order, to prove correlation (not
                # send order) determines which client gets which answer.
                first = json.loads(await node_ws.recv())
                second = json.loads(await node_ws.recv())
                await node_ws.send(json.dumps({
                    "type": "complete_result", "request_id": second["request_id"],
                    "text": f"reply to: {second['prompt']}",
                }))
                await node_ws.send(json.dumps({
                    "type": "complete_result", "request_id": first["request_id"],
                    "text": f"reply to: {first['prompt']}",
                }))

            node_task = asyncio.create_task(flaky_reversing_node())

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_a:
                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_b:
                    await client_a.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "A"}
                    ))
                    await client_b.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "B"}
                    ))
                    response_a = json.loads(await client_a.recv())
                    response_b = json.loads(await client_b.recv())

            await node_task
            assert response_a == {"type": "complete_result", "text": "reply to: A"}
            assert response_b == {"type": "complete_result", "text": "reply to: B"}


async def test_complete_request_round_robins_across_two_healthy_nodes(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_a_ws:
            await node_a_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_a_ws.recv()
            node_a_task = asyncio.create_task(_run_fake_node(
                node_a_ws, lambda msg: {"type": "complete_result", "text": f"node-a: {msg['prompt']}"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b_ws:
                await node_b_ws.send(json.dumps(
                    {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-b"}
                ))
                await node_b_ws.recv()
                node_b_task = asyncio.create_task(_run_fake_node(
                    node_b_ws, lambda msg: {"type": "complete_result", "text": f"node-b: {msg['prompt']}"}
                ))

                # Two separate connections, one per request: a client
                # connection is one-shot (the server closes it after
                # replying to a "complete" — see _handle_complete_request),
                # so a second request on the same connection would find it
                # already closed.
                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws_1:
                    await client_ws_1.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "1"}
                    ))
                    first = json.loads(await client_ws_1.recv())

                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws_2:
                    await client_ws_2.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "2"}
                    ))
                    second = json.loads(await client_ws_2.recv())

                assert {first["text"], second["text"]} == {"node-a: 1", "node-b: 2"}

            node_a_task.cancel()
            node_b_task.cancel()


async def test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead():
    registry = NodeRegistry("secret-token")
    dead_ws = _FakeNodeWebsocket(
        send_raises=websockets.exceptions.ConnectionClosedError(None, None)
    )
    healthy_ws = _FakeNodeWebsocket()
    registry.register("node-a", "m", dead_ws)  # registers first -> round robin picks it first
    registry.register("node-b", "m", healthy_ws)

    async def reply_from_node_b():
        while not healthy_ws.sent:
            await asyncio.sleep(0.01)
        sent = json.loads(healthy_ws.sent[0])
        node_b = registry.get("node-b")
        node_b.pending[sent["request_id"]].set_result(
            {"type": "complete_result", "text": "answer from node-b", "request_id": sent["request_id"]}
        )

    asyncio.create_task(reply_from_node_b())

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    assert json.loads(client_ws.sent[0]) == {"type": "complete_result", "text": "answer from node-b"}
    # node-a's dead connection must have been self-healed out of the registry.
    assert registry.list_nodes() == [{"node_id": "node-b", "model": "m"}]


async def test_complete_request_does_not_fail_over_on_timeout(monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 0.2)
    registry = NodeRegistry("secret-token")
    slow_ws = _FakeNodeWebsocket()  # accepts the send, never replies
    other_ws = _FakeNodeWebsocket()
    registry.register("node-a", "m", slow_ws)
    registry.register("node-b", "m", other_ws)

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    response = json.loads(client_ws.sent[0])
    assert response["type"] == "complete_error"
    assert other_ws.sent == []  # node-b was never contacted
    # A timeout isn't treated as a dead node — node-a stays registered.
    assert registry.list_nodes() == [
        {"node_id": "node-a", "model": "m"},
        {"node_id": "node-b", "model": "m"},
    ]


async def test_complete_request_returns_error_when_every_node_is_dead():
    registry = NodeRegistry("secret-token")
    dead_a = _FakeNodeWebsocket(send_raises=websockets.exceptions.ConnectionClosedError(None, None))
    dead_b = _FakeNodeWebsocket(send_raises=websockets.exceptions.ConnectionClosedError(None, None))
    registry.register("node-a", "m", dead_a)
    registry.register("node-b", "m", dead_b)

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    response = json.loads(client_ws.sent[0])
    assert response["type"] == "complete_error"
    assert registry.list_nodes() == []
