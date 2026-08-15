"""Tests for mycelium.node.registration."""

import asyncio
import json
import ssl

import pytest
import websockets

from mycelium.coordinator import certs
from mycelium.node import connection, registration


def _server_ssl_context(cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


async def test_register_succeeds_and_returns_on_registered_response(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    received = {}

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await registration.register(
                ws, token="secret", model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a"
            )

    assert received == {
        "type": "register",
        "token": "secret",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "node_id": "node-a",
    }


async def test_register_raises_rejected_on_registration_rejected_response(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async def fake_coordinator(websocket):
        await websocket.recv()
        await websocket.send(json.dumps({"type": "registration_rejected", "reason": "invalid token"}))

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            with pytest.raises(registration.RegistrationRejected, match="invalid token"):
                await registration.register(ws, token="bad", model="m", node_id="node-a")


async def test_register_raises_timeout_when_coordinator_never_responds(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async def silent_coordinator(websocket):
        await websocket.recv()
        await asyncio.sleep(60)  # never responds within the test's short timeout

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(silent_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            with pytest.raises(registration.RegistrationTimeout):
                await registration.register(ws, token="secret", model="m", node_id="node-a", timeout=0.5)


async def test_register_raises_on_unexpected_response_type(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async def confused_coordinator(websocket):
        await websocket.recv()
        await websocket.send(json.dumps({"type": "something_else"}))

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(confused_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            with pytest.raises(registration.RegistrationRejected):
                await registration.register(ws, token="secret", model="m", node_id="node-a")
