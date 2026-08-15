"""End-to-end local integration test: cert generation, server, and the node's
reconnecting client, all together. Real two-machine verification (a real
coordinator host and real node hardware) happens separately — see the
design doc and plan for issue #5 — this test only proves the pieces wire
up correctly on localhost before that.
"""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import websockets

from mycelium.client.cli import complete as client_complete
from mycelium.coordinator import certs, server
from mycelium.node import connection, registration, request_handler
from mycelium.node.vllm_process import VLLMProcess


async def test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    def fast_delays():
        while True:
            yield 0.1

    connect_count = 0
    stop = asyncio.Event()

    async def node_loop(port):
        nonlocal connect_count
        async for websocket in connection.connect(
            f"wss://127.0.0.1:{port}", cert_path, reconnect_delays_factory=fast_delays
        ):
            connect_count += 1
            await registration.register(websocket, token="secret-token", model="m", node_id="node-a")
            try:
                await websocket.wait_closed()
            except websockets.exceptions.ConnectionClosed:
                if stop.is_set():
                    return
                continue

    coordinator1 = await server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token")
    port = coordinator1.sockets[0].getsockname()[1]
    node_task = asyncio.create_task(node_loop(port))

    await asyncio.sleep(0.5)
    assert connect_count == 1, "node should have connected once to the first coordinator"

    coordinator1.close()
    await coordinator1.wait_closed()
    await asyncio.sleep(0.5)  # let the node notice the drop

    coordinator2 = await server.serve("127.0.0.1", port, cert_path, key_path, "secret-token")
    await asyncio.sleep(0.5)  # let the node reconnect

    stop.set()
    node_task.cancel()
    coordinator2.close()
    await coordinator2.wait_closed()

    assert connect_count == 2, f"expected exactly 2 connect attempts, got {connect_count}"


def test_server_and_connection_agree_on_keepalive_settings():
    assert server.PING_INTERVAL_SECONDS == connection.PING_INTERVAL_SECONDS
    assert server.PING_TIMEOUT_SECONDS == connection.PING_TIMEOUT_SECONDS


def test_server_and_registration_agree_on_timeout_settings():
    assert server.FIRST_MESSAGE_TIMEOUT_SECONDS == registration.REGISTRATION_TIMEOUT_SECONDS


class _FakeVLLMHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            prompt = body["messages"][0]["content"]
            reply = json.dumps(
                {"choices": [{"message": {"content": f"real completion for: {prompt}"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


async def test_full_round_trip_client_through_coordinator_to_node_and_back(tmp_path):
    """Simulated end-to-end proof of Phase 0 success criterion #2: a real
    client, a real coordinator, a real node registration+dispatch loop,
    and a real (fake-vLLM-backed) VLLMProcess, all wired together as they
    would be in production — only the vLLM HTTP server itself is faked,
    matching #7/#8's existing test pattern for exercising VLLMProcess
    without real GPU hardware. Live-hardware verification (Task 9) is what
    proves the real vLLM piece; this test proves the wiring."""
    fake_vllm = HTTPServer(("127.0.0.1", 0), _FakeVLLMHandler)
    vllm_thread = Thread(target=fake_vllm.serve_forever, daemon=True)
    vllm_thread.start()
    try:
        process = VLLMProcess(
            model="m", gpu="0", port=fake_vllm.server_address[1]
        )

        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        certs.ensure_cert(cert_path, key_path, "127.0.0.1")

        async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
            port = coordinator.sockets[0].getsockname()[1]

            async def node_loop():
                async for websocket in connection.connect(f"wss://127.0.0.1:{port}", cert_path):
                    await registration.register(
                        websocket, token="secret-token", model="m", node_id="node-a"
                    )
                    await request_handler.handle_messages(websocket, process)

            node_task = asyncio.create_task(node_loop())
            await asyncio.sleep(0.3)  # let the node connect and register

            text = await client_complete(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "m", "what's the capital?"
            )

            node_task.cancel()
            try:
                await node_task
            except asyncio.CancelledError:
                pass

    finally:
        fake_vllm.shutdown()
        vllm_thread.join()

    assert text == "real completion for: what's the capital?"
