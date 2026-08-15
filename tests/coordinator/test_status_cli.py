"""Tests for mycelium.coordinator.status_cli."""

import ssl

import pytest
import websockets

from mycelium.coordinator import certs, server
from mycelium.coordinator.status_cli import QueryError, parse_args, query_status
from mycelium.node import registration


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


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

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        nodes = await query_status(f"wss://127.0.0.1:{port}", cert_path, "secret-token")

    assert nodes == []


async def test_query_status_returns_registered_node(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            await registration.register(
                node_ws, token="secret-token", model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a"
            )

            nodes = await query_status(f"wss://127.0.0.1:{port}", cert_path, "secret-token")

    assert nodes == [{"node_id": "node-a", "model": "Qwen/Qwen2.5-7B-Instruct"}]


async def test_query_status_raises_on_wrong_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(QueryError, match="rejected"):
            await query_status(f"wss://127.0.0.1:{port}", cert_path, "wrong-token")
