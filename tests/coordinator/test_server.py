"""Tests for mycelium.coordinator.server."""

import asyncio
import json
import ssl

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
