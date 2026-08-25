"""Tests for mycelium.coordinator.status_cli."""

import ssl
import sys

import pytest
import websockets

from mycelium import crypto
from mycelium.coordinator import certs, server, status_cli
from mycelium.coordinator import github_identity
from mycelium.coordinator.status_cli import QueryError, parse_args, query_status
from mycelium.node import registration


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")


def test_parse_args_requires_all_three_flags():
    with pytest.raises(SystemExit):
        parse_args([])


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
        ]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == str(cert_path)
    assert str(args.token_file) == str(token_file)


async def test_query_status_returns_empty_list_when_no_nodes_registered(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        nodes = await query_status(f"wss://127.0.0.1:{port}", cert_path, "secret-token")

    assert nodes == []


async def test_query_status_returns_registered_node(tmp_path):
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
            public_key = crypto.public_key_b64(private_key)
            await registration.register(
                node_ws, model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a",
                public_key=public_key, signature=crypto.sign_public_key(private_key),
                github_token="valid-github-token",
            )

            nodes = await query_status(f"wss://127.0.0.1:{port}", cert_path, "secret-token")

    assert nodes == [
        {
            "node_id": "node-a",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "fingerprint": crypto.fingerprint(public_key),
            "identity": "octocat",
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
        }
    ]


def test_main_prints_bound_github_identity(tmp_path, monkeypatch, capsys):
    """See Finding 3 of the final whole-branch review for issue #39:
    list_nodes() already returns an "identity" field, but main()'s print
    loop never displayed it. query_status() is faked here so this test
    exercises main()'s own output formatting, not the network path
    (already covered by test_query_status_returns_registered_node)."""
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")

    async def fake_query_status(coordinator_url, coordinator_cert, token):
        return [
            {
                "node_id": "node-a",
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "fingerprint": "a1b2c3d4e5f6",
                "identity": "octocat",
                "reputation": {"completions": 12, "timeouts": 1, "crashes": 0, "disconnects": 2},
            }
        ]

    monkeypatch.setattr(status_cli, "query_status", fake_query_status)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mycelium-coordinator-status",
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ],
    )

    status_cli.main()

    out = capsys.readouterr().out
    assert out == (
        "node-a [a1b2c3d4e5f6] (github:octocat) [ok:12 timeout:1 crash:0 disconnect:2]: "
        "Qwen/Qwen2.5-7B-Instruct\n"
    )


def test_main_omits_identity_suffix_when_node_has_none(tmp_path, monkeypatch, capsys):
    """A node registered without identity resolution (only possible via a
    direct NodeRegistry.register() call, never in production) has
    identity: None — main() must not print a bogus "(github:None)"."""
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")

    async def fake_query_status(coordinator_url, coordinator_cert, token):
        return [
            {
                "node_id": "node-a",
                "model": "m",
                "fingerprint": "a1b2c3d4e5f6",
                "identity": None,
                "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
            }
        ]

    monkeypatch.setattr(status_cli, "query_status", fake_query_status)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mycelium-coordinator-status",
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ],
    )

    status_cli.main()

    out = capsys.readouterr().out
    assert out == "node-a [a1b2c3d4e5f6] [ok:0 timeout:0 crash:0 disconnect:0]: m\n"


async def test_query_status_raises_on_wrong_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(QueryError, match="rejected"):
            await query_status(f"wss://127.0.0.1:{port}", cert_path, "wrong-token")
