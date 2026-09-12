"""Tests for mycelium.client.models_cli."""

import json
import ssl
import sys

import pytest
import websockets

from mycelium import crypto
from mycelium.client import models_cli, transport
from mycelium.coordinator import certs, github_identity, server


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")


def test_parse_args_requires_the_connection_flags():
    with pytest.raises(SystemExit):
        models_cli.parse_args([])


async def test_list_models_returns_what_the_coordinator_serves(tmp_path):
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
                "type": "register", "model": "model-one", "node_id": "node-a",
                "public_key": crypto.public_key_b64(private_key),
                "signature": crypto.sign_public_key(private_key),
                "github_token": "valid-github-token",
            }))
            await node_ws.recv()

            models = await models_cli.list_models(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token"
            )

    assert models == [{"model": "model-one", "healthy_nodes": 1}]


async def test_list_models_raises_on_a_rejected_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(models_cli.QueryError):
            await models_cli.list_models(
                f"wss://127.0.0.1:{port}", cert_path, "wrong-token"
            )


async def test_list_models_raises_query_error_on_a_malformed_reply(monkeypatch):
    """A reply with the right `type` but an entry missing `healthy_nodes`
    used to raise KeyError from main()'s print loop — a traceback where a
    wrong-type reply already gets a clean QueryError. Both must be
    treated the same way."""

    async def fake_request(coordinator_url, coordinator_cert, message, timeout):
        return {"type": "models", "models": [{"model": "m"}]}

    monkeypatch.setattr(transport, "request", fake_request)

    with pytest.raises(models_cli.QueryError):
        await models_cli.list_models("wss://example:8765", "cert.pem", "secret-token")


async def test_list_models_goes_through_the_shared_transport(monkeypatch):
    """The three tests above would still pass if list_models hand-rolled
    its own connect-send-receive — exactly the duplication this task
    exists to prevent. This one pins the actual mechanism: transport.request
    is called, and with the expected {"type": "list_models", ...} payload."""
    captured = {}

    async def fake_request(coordinator_url, coordinator_cert, message, timeout):
        captured["coordinator_url"] = coordinator_url
        captured["coordinator_cert"] = coordinator_cert
        captured["message"] = message
        captured["timeout"] = timeout
        return {"type": "models", "models": [{"model": "m", "healthy_nodes": 1}]}

    monkeypatch.setattr(transport, "request", fake_request)

    models = await models_cli.list_models("wss://example:8765", "cert.pem", "secret-token")

    assert captured["message"] == {"type": "list_models", "token": "secret-token"}
    assert models == [{"model": "m", "healthy_nodes": 1}]


def test_main_prints_an_error_and_exits_nonzero_when_the_token_file_is_missing(
    tmp_path, monkeypatch, capsys
):
    """args.token_file.read_text() used to run before the try block, so a
    missing (or unreadable, or directory-as-path) token file raised
    FileNotFoundError straight at the user instead of a clean error message
    and exit 1."""
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    missing_token_file = tmp_path / "does-not-exist"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mycelium-client-models",
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(missing_token_file),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        models_cli.main()

    assert exc_info.value.code != 0
    out = capsys.readouterr().out
    assert out.startswith("error: ")


def test_main_exits_nonzero_and_prints_a_message_on_a_rejected_token(
    tmp_path, monkeypatch, capsys
):
    """Verifies main()'s own exit-status contract, not just list_models():
    a QueryError must reach the user as a message on stdout and a non-zero
    exit, never a traceback."""
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("wrong-token")

    async def fake_list_models(coordinator_url, coordinator_cert, token):
        raise models_cli.QueryError(
            "coordinator closed the connection without responding (check the token)"
        )

    monkeypatch.setattr(models_cli, "list_models", fake_list_models)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mycelium-client-models",
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        models_cli.main()

    assert exc_info.value.code != 0
    out = capsys.readouterr().out
    assert out == (
        "error: coordinator closed the connection without responding "
        "(check the token)\n"
    )
