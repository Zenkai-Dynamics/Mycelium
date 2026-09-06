"""Tests for mycelium.client.models_cli."""

import json
import ssl

import pytest
import websockets

from mycelium import crypto
from mycelium.client import models_cli
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
