"""End-to-end local integration test: cert generation, server, and the node's
reconnecting client, all together. Real two-machine verification (a real
coordinator host and real node hardware) happens separately — see the
design doc and plan for issue #5 — this test only proves the pieces wire
up correctly on localhost before that.
"""

import asyncio

import websockets

from mycelium.coordinator import certs, server
from mycelium.node import connection


async def test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    def fast_delays():
        while True:
            yield 0.1

    connect_count = 0
    stop = asyncio.Event()

    async def node_loop():
        nonlocal connect_count
        async for websocket in connection.connect(
            "wss://127.0.0.1:8995", cert_path, reconnect_delays_factory=fast_delays
        ):
            connect_count += 1
            try:
                await websocket.wait_closed()
            except websockets.exceptions.ConnectionClosed:
                if stop.is_set():
                    return
                continue

    node_task = asyncio.create_task(node_loop())

    coordinator1 = await server.serve("127.0.0.1", 8995, cert_path, key_path)
    await asyncio.sleep(0.5)
    assert connect_count == 1, "node should have connected once to the first coordinator"

    coordinator1.close()
    await coordinator1.wait_closed()
    await asyncio.sleep(0.5)  # let the node notice the drop

    coordinator2 = await server.serve("127.0.0.1", 8995, cert_path, key_path)
    await asyncio.sleep(0.5)  # let the node reconnect

    stop.set()
    node_task.cancel()
    coordinator2.close()
    await coordinator2.wait_closed()

    assert connect_count == 2, f"expected exactly 2 connect attempts, got {connect_count}"
