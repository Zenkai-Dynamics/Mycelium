"""Tests for mycelium.coordinator.server."""

import ssl

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

    async with server.serve("127.0.0.1", 8981, cert_path, key_path):
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect("wss://127.0.0.1:8981", ssl=client_ctx) as ws:
            assert ws.state.name == "OPEN"


async def test_multiple_nodes_can_connect_simultaneously(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 8982, cert_path, key_path):
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect("wss://127.0.0.1:8982", ssl=client_ctx) as ws1:
            async with websockets.connect("wss://127.0.0.1:8982", ssl=client_ctx) as ws2:
                assert ws1.state.name == "OPEN"
                assert ws2.state.name == "OPEN"


async def test_connection_with_wrong_pinned_cert_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    # A second, unrelated cert stands in for "wrong pinned cert."
    other_cert_path = tmp_path / "other-cert.pem"
    other_key_path = tmp_path / "other-key.pem"
    certs.ensure_cert(other_cert_path, other_key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 8983, cert_path, key_path):
        wrong_ctx = _client_ssl_context(other_cert_path)
        try:
            async with websockets.connect("wss://127.0.0.1:8983", ssl=wrong_ctx):
                assert False, "expected connection to be rejected"
        except ssl.SSLCertVerificationError:
            pass


async def test_server_survives_abnormal_disconnect(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 8984, cert_path, key_path):
        client_ctx = _client_ssl_context(cert_path)
        ws = await websockets.connect("wss://127.0.0.1:8984", ssl=client_ctx)
        assert ws.state.name == "OPEN"
        # Forcibly tear down the transport instead of a clean close handshake,
        # to simulate a node being killed / network drop.
        ws.transport.close()

        # The server must still be accepting new connections afterward.
        async with websockets.connect("wss://127.0.0.1:8984", ssl=client_ctx) as ws2:
            assert ws2.state.name == "OPEN"
