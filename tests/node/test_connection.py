"""Tests for mycelium.node.connection."""

import asyncio
import itertools
import ssl

import websockets

from mycelium.coordinator import certs
from mycelium.node import connection


def _server_ssl_context(cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


async def _echo_handler(websocket):
    async for message in websocket:
        await websocket.send(message)


def test_reconnect_delays_starts_near_one_second():
    delays = list(itertools.islice(connection.reconnect_delays(), 1))
    assert 0.8 <= delays[0] <= 1.2


def test_reconnect_delays_grows_and_caps_at_thirty_seconds():
    delays = list(itertools.islice(connection.reconnect_delays(), 10))
    # Roughly doubling early on (within jitter bounds)...
    assert 1.6 <= delays[1] <= 2.4
    assert 3.2 <= delays[2] <= 4.8
    # ...and capped at ~30s by the time it's had several steps to grow there.
    assert 24.0 <= delays[-1] <= 36.0


async def test_connects_to_real_server(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(_echo_handler, "127.0.0.1", 8991, ssl=server_ctx):
        async for websocket in connection.connect("wss://127.0.0.1:8991", cert_path):
            assert websocket.state.name == "OPEN"
            break  # one connection is enough to prove this works


async def test_reconnects_after_server_restart(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    server_ctx = _server_ssl_context(cert_path, key_path)

    def fast_delays():
        while True:
            yield 0.1

    connect_count = 0

    async def client_loop():
        nonlocal connect_count
        async for websocket in connection.connect(
            "wss://127.0.0.1:8992", cert_path, reconnect_delays_factory=fast_delays
        ):
            connect_count += 1
            try:
                await websocket.wait_closed()
            except websockets.exceptions.ConnectionClosed:
                continue

    client_task = asyncio.create_task(client_loop())

    server1 = await websockets.serve(_echo_handler, "127.0.0.1", 8992, ssl=server_ctx)
    await asyncio.sleep(1)
    server1.close()
    await server1.wait_closed()

    await asyncio.sleep(1.5)  # let the client notice the drop and start retrying

    server2 = await websockets.serve(_echo_handler, "127.0.0.1", 8992, ssl=server_ctx)
    await asyncio.sleep(1.5)  # let the client reconnect

    client_task.cancel()
    server2.close()
    await server2.wait_closed()

    assert connect_count >= 2, f"expected at least 2 connect attempts, got {connect_count}"
