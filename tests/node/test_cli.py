"""Tests for mycelium.node.cli."""

import asyncio
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
import websockets

from mycelium.coordinator import certs
from mycelium.node import vllm_process
from mycelium.node.cli import _run, parse_args

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parse_args_partial_coordinator_args_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--coordinator-cert", "/tmp/cert.pem"])
    with pytest.raises(SystemExit):
        parse_args(["--coordinator-url", "wss://example:8765"])


def test_parse_args_requires_coordinator_or_prompt():
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_prompt_alone_is_valid():
    args = parse_args(["--prompt", "hello"])
    assert args.prompt == "hello"
    assert args.coordinator_url is None
    assert args.coordinator_cert is None
    assert args.token_file is None


def test_parse_args_coordinator_requires_token_file(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    with pytest.raises(SystemExit):
        parse_args(
            ["--coordinator-url", "wss://example:8765", "--coordinator-cert", str(cert_path)]
        )


def test_parse_args_coordinator_alone_is_valid(tmp_path):
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
    assert args.prompt is None


def test_parse_args_defaults():
    args = parse_args(["--prompt", "hi"])
    assert args.model == vllm_process.DEFAULT_MODEL
    assert args.gpu == vllm_process.DEFAULT_GPU
    assert args.vllm_port == vllm_process.DEFAULT_PORT
    assert args.node_id is None


def test_parse_args_overrides():
    args = parse_args(
        [
            "--prompt", "hi",
            "--model", "some/other-model",
            "--gpu", "1",
            "--vllm-port", "9000",
            "--node-id", "my-node",
        ]
    )
    assert args.model == "some/other-model"
    assert args.gpu == "1"
    assert args.vllm_port == 9000
    assert args.node_id == "my-node"


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
            self.rfile.read(length)
            body = json.dumps(
                {"choices": [{"message": {"content": "fake completion"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture
def fake_vllm_server():
    server = HTTPServer(("127.0.0.1", 0), _FakeVLLMHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_run_prompt_mode_forwards_prompt_and_prints_completion(
    monkeypatch, capsys, fake_vllm_server
):
    port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    args = parse_args(["--prompt", "what is the answer?", "--vllm-port", str(port)])
    process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    await _run(args, process)

    assert "fake completion" in capsys.readouterr().out


def _server_ssl_context(cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


async def test_run_registers_with_coordinator_using_token_and_node_id(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    token_file = tmp_path / "token"
    token_file.write_text("secret-token\n")

    received = {}
    registered_event = asyncio.Event()

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))
        registered_event.set()
        await websocket.wait_closed()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--token-file", str(token_file),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(_run(args, process))
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert received == {
        "type": "register",
        "token": "secret-token",
        "model": vllm_process.DEFAULT_MODEL,
        "node_id": "test-node",
    }


async def test_run_retries_after_registration_rejected(tmp_path, monkeypatch, fake_vllm_server):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    token_file = tmp_path / "token"
    token_file.write_text("wrong-token\n")

    attempt_count = 0

    async def rejecting_coordinator(websocket):
        nonlocal attempt_count
        attempt_count += 1
        await websocket.recv()
        await websocket.send(json.dumps({"type": "registration_rejected", "reason": "invalid token"}))
        await websocket.close()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(rejecting_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--token-file", str(token_file),
                "--vllm-port", str(vllm_port),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(_run(args, process))
        await asyncio.sleep(2.5)  # let it attempt, get rejected, back off (~1s), attempt again
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert attempt_count >= 2


async def test_run_rejects_empty_token_file_before_starting_vllm(tmp_path, monkeypatch, fake_vllm_server):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("  \n")

    args = parse_args(
        [
            "--coordinator-url", "wss://127.0.0.1:1",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--vllm-port", str(vllm_port),
        ]
    )
    process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    with pytest.raises(SystemExit, match="empty"):
        await _run(args, process)

    # vLLM must never have been started — the empty-token check happens
    # before process.start(), so there's nothing to clean up here and no
    # subprocess was spawned.


def test_sigterm_stops_vllm_process_group_with_no_orphans(tmp_path):
    """Regression test for the SIGTERM/SIGHUP orphan bug found in final
    review: drives the real CLI as an OS subprocess (not a direct function
    call) so an actual signal is what triggers cleanup, via a `vllm` shim
    on PATH that spawns a child process the way vLLM spawns its own worker
    — proving a bare-PID kill would leave that child behind."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()

    shim = bin_dir / "vllm"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import http.server, os, subprocess, sys\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "pid_dir = os.environ['FAKE_VLLM_PID_DIR']\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "open(os.path.join(pid_dir, 'parent'), 'w').write(str(os.getpid()))\n"
        "open(os.path.join(pid_dir, 'child'), 'w').write(str(child.pid))\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    shim.chmod(0o755)

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    token_file = tmp_path / "token"
    token_file.write_text("unused-token\n")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_VLLM_PID_DIR"] = str(pid_dir)
    vllm_port = _free_port()

    node_proc = subprocess.Popen(
        [
            sys.executable, "-m", "mycelium.node.cli",
            "--coordinator-url", "wss://127.0.0.1:1",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--vllm-port", str(vllm_port),
        ],
        env=env,
    )
    parent_pid = None
    child_pid = None
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if (pid_dir / "parent").exists() and (pid_dir / "child").exists():
                break
            time.sleep(0.2)
        else:
            pytest.fail("fake vllm never reported its PIDs")

        parent_pid = int((pid_dir / "parent").read_text())
        child_pid = int((pid_dir / "child").read_text())
        assert _process_alive(parent_pid)
        assert _process_alive(child_pid)

        node_proc.send_signal(signal.SIGTERM)
        node_proc.wait(timeout=15.0)
    finally:
        if node_proc.poll() is None:
            node_proc.kill()
            node_proc.wait()

    assert parent_pid is not None and child_pid is not None
    time.sleep(0.5)
    assert not _process_alive(parent_pid)
    assert not _process_alive(child_pid)
