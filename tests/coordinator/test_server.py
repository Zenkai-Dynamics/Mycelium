"""Tests for mycelium.coordinator.server."""

import asyncio
import json
import ssl
import time

import pytest
import websockets

from mycelium.coordinator import certs, server


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
            + 0.3
        )

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []
