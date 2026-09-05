"""Tests for mycelium.client.transport."""

import asyncio
import json
import ssl
from pathlib import Path

import pytest
import websockets

from mycelium import crypto
from mycelium.client import transport
from mycelium.coordinator import certs, github_identity, server


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")


async def test_request_returns_the_parsed_reply(tmp_path):
    """A status_query is used deliberately: transport must work for any
    request type, since it knows nothing about completions."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]

        reply = await transport.request(
            f"wss://127.0.0.1:{port}", cert_path,
            {"type": "status_query", "token": "secret-token"}, timeout=5.0,
        )

    assert reply["type"] == "status"
    assert reply["nodes"] == []


async def test_request_returns_a_complete_error_as_data(tmp_path):
    """A complete_error is a reply, not an exception — interpreting it is
    the caller's job, not transport's."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]

        reply = await transport.request(
            f"wss://127.0.0.1:{port}", cert_path,
            {"type": "complete", "token": "secret-token", "model": "nope", "prompt": "hi"},
            timeout=5.0,
        )

    assert reply["type"] == "complete_error"
    assert "no healthy node" in reply["reason"]


async def test_request_raises_when_the_coordinator_closes_without_replying(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]

        with pytest.raises(transport.TransportError, match="without responding"):
            await transport.request(
                f"wss://127.0.0.1:{port}", cert_path,
                {"type": "complete", "token": "wrong-token", "model": "m", "prompt": "hi"},
                timeout=5.0,
            )


async def test_request_raises_on_timeout(tmp_path):
    """A registered node that never answers: the coordinator holds the
    client connection open, so transport's own timeout must fire.

    A bare 1ms timeout racing a local round trip proved to reliably lose
    that race on this machine rather than reliably win it, so per the
    brief's documented fallback this drives the timeout with a registered
    node that never replies instead — the coordinator's own internal
    NODE_COMPLETE_TIMEOUT_SECONDS (130s) is far longer than the 0.2s given
    here, so transport's own timeout is what fires."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            private_key = crypto.generate_keypair()
            await node_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": crypto.public_key_b64(private_key),
                "signature": crypto.sign_public_key(private_key),
                "github_token": "valid-github-token",
            }))
            await node_ws.recv()

            # Receives the routed "complete" but never replies.
            node_task = asyncio.create_task(node_ws.recv())

            with pytest.raises(transport.TransportError, match="did not respond"):
                await transport.request(
                    f"wss://127.0.0.1:{port}", cert_path,
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"},
                    timeout=0.2,
                )

            await node_task


async def test_request_raises_when_the_handshake_is_rejected(tmp_path):
    """A TLS endpoint that is not a websocket server — the wrong port, or
    a proxy in front of the coordinator — answers the upgrade with plain
    HTTP. websockets raises InvalidStatus, which derives from
    WebSocketException and *not* from OSError, so before issue #59's fix
    it escaped transport entirely: Flow could not record the attempted
    hop and mycelium-client printed a traceback."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async def handle(reader, writer):
        await reader.readline()
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    not_a_websocket_server = await asyncio.start_server(
        handle, "127.0.0.1", 0, ssl=server.build_ssl_context(cert_path, key_path)
    )
    port = not_a_websocket_server.sockets[0].getsockname()[1]

    try:
        with pytest.raises(transport.TransportError):
            await transport.request(
                f"wss://127.0.0.1:{port}", cert_path,
                {"type": "status_query", "token": "secret-token"}, timeout=5.0,
            )
    finally:
        not_a_websocket_server.close()
        await not_a_websocket_server.wait_closed()


async def test_request_raises_when_the_reply_is_not_json(tmp_path):
    """Parsing is part of the round trip transport promises to complete,
    so a reply that is not JSON must arrive as a TransportError like any
    other failure to produce a reply — not as a JSONDecodeError from a
    line the caller never sees."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async def handler(websocket):
        await websocket.recv()
        await websocket.send("this is not json")
        await websocket.close()

    running = await websockets.serve(
        handler, "127.0.0.1", 0, ssl=server.build_ssl_context(cert_path, key_path)
    )
    port = running.sockets[0].getsockname()[1]

    try:
        with pytest.raises(transport.TransportError):
            await transport.request(
                f"wss://127.0.0.1:{port}", cert_path,
                {"type": "status_query", "token": "secret-token"}, timeout=5.0,
            )
    finally:
        running.close()
        await running.wait_closed()


async def test_request_raises_when_the_coordinator_cannot_be_reached(tmp_path):
    """A refused connection raises OSError out of websockets, not
    ConnectionClosed. Without wrapping it, a bare OSError would escape
    transport and Flow could not record the attempted hop."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    with pytest.raises(transport.TransportError, match="could not reach"):
        await transport.request(
            "wss://127.0.0.1:1", cert_path,
            {"type": "status_query", "token": "secret-token"}, timeout=5.0,
        )
