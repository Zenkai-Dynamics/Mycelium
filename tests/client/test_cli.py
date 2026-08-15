"""Tests for mycelium.client.cli."""

import asyncio
import json
import ssl

import pytest
import websockets

from mycelium.coordinator import certs, server
from mycelium.client.cli import CompletionError, complete, parse_args


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


def test_parse_args_requires_all_flags():
    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args(["--model", "m", "--prompt", "hi"])


def test_parse_args_valid(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    args = parse_args(
        [
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--model", "Qwen/Qwen2.5-7B-Instruct",
            "--prompt", "hello",
        ]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == str(cert_path)
    assert str(args.token_file) == str(token_file)
    assert args.model == "Qwen/Qwen2.5-7B-Instruct"
    assert args.prompt == "hello"


async def test_complete_returns_text_on_success(tmp_path):
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

            async def fake_node():
                raw = await node_ws.recv()
                msg = json.loads(raw)
                await node_ws.send(json.dumps({
                    "type": "complete_result",
                    "request_id": msg["request_id"],
                    "text": f"echo: {msg['prompt']}",
                }))

            node_task = asyncio.create_task(fake_node())

            text = await complete(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "m", "hello"
            )
            await node_task

    assert text == "echo: hello"


async def test_complete_raises_on_error_reply(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(CompletionError, match="no healthy node"):
            await complete(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "no-such-model", "hi"
            )


async def test_complete_raises_on_wrong_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(CompletionError):
            await complete(f"wss://127.0.0.1:{port}", cert_path, "wrong-token", "m", "hi")
