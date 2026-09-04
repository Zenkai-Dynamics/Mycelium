"""Tests for mycelium.client.cli."""

import asyncio
import json
import ssl
from pathlib import Path

import pytest
import websockets

from mycelium import crypto
from mycelium.coordinator import certs, server
from mycelium.coordinator import github_identity
from mycelium.client import cli
from mycelium.client.cli import CompletionError, complete, parse_args


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")


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

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
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

            async def fake_node():
                raw = await node_ws.recv()
                msg = json.loads(raw)
                await node_ws.send(json.dumps({
                    "type": "complete_result",
                    "request_id": msg["request_id"],
                    "text": f"echo: {msg['messages'][-1]['content']}",
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


async def test_complete_sends_a_messages_array_when_given_one(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    received: list[dict] = []

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

            async def fake_node():
                msg = json.loads(await node_ws.recv())
                received.append(msg)
                await node_ws.send(json.dumps({
                    "type": "complete_result",
                    "request_id": msg["request_id"],
                    "text": "the answer",
                }))

            node_task = asyncio.create_task(fake_node())

            text = await complete(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "m", messages=messages
            )
            await node_task

    assert text == "the answer"
    assert received[0]["messages"] == messages
    assert "prompt" not in received[0]


async def test_complete_rejects_both_prompt_and_messages_before_connecting():
    """No coordinator is started at all: an ambiguous request must fail
    locally, so an unreachable URL is never dialed."""
    with pytest.raises(CompletionError) as exc:
        await complete(
            "wss://127.0.0.1:1", Path("nonexistent.pem"), "secret-token", "m",
            prompt="hi", messages=[{"role": "user", "content": "hi"}],
        )
    assert "both" in str(exc.value).lower()


async def test_complete_rejects_neither_prompt_nor_messages():
    with pytest.raises(CompletionError):
        await complete("wss://127.0.0.1:1", Path("nonexistent.pem"), "secret-token", "m")


def test_parse_args_rejects_prompt_and_messages_file_together(tmp_path):
    messages_file = tmp_path / "conv.json"
    messages_file.write_text("[]")
    with pytest.raises(SystemExit):
        cli.parse_args([
            "--coordinator-url", "wss://x", "--coordinator-cert", "c.pem",
            "--token-file", "t.txt", "--model", "m",
            "--prompt", "hi", "--messages-file", str(messages_file),
        ])


def test_parse_args_requires_one_of_prompt_or_messages_file():
    with pytest.raises(SystemExit):
        cli.parse_args([
            "--coordinator-url", "wss://x", "--coordinator-cert", "c.pem",
            "--token-file", "t.txt", "--model", "m",
        ])
