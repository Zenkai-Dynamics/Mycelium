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
from mycelium.node import github_device_flow, vllm_process
from mycelium.node.cli import _authenticate, _run, parse_args

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
    assert args.github_token_file is None


def test_parse_args_coordinator_alone_is_valid_without_github_token_file(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    args = parse_args(
        ["--coordinator-url", "wss://example:8765", "--coordinator-cert", str(cert_path)]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert args.github_token_file is None


def test_parse_args_coordinator_alone_is_valid(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    args = parse_args(
        [
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
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


def test_parse_args_node_key_file_default():
    from mycelium.node import identity
    args = parse_args(["--prompt", "hi"])
    assert args.node_key_file == identity.DEFAULT_KEY_PATH


def test_parse_args_node_key_file_override():
    args = parse_args(["--prompt", "hi", "--node-key-file", "/tmp/custom-key.pem"])
    assert str(args.node_key_file) == "/tmp/custom-key.pem"


def test_parse_args_github_token_file_override(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    github_token_file = tmp_path / "github-token"
    github_token_file.write_text("gh-secret")
    args = parse_args(
        [
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--github-token-file", str(github_token_file),
        ]
    )
    assert str(args.github_token_file) == str(github_token_file)


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


async def test_run_registers_with_coordinator_using_github_token_and_node_id(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    github_token_file = tmp_path / "github-token"
    github_token_file.write_text("gh-secret-token\n")

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
                "--github-token-file", str(github_token_file),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
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

    from mycelium import crypto

    assert received["type"] == "register"
    assert received["github_token"] == "gh-secret-token"
    assert received["model"] == vllm_process.DEFAULT_MODEL
    assert received["node_id"] == "test-node"
    assert crypto.verify_registration_signature(received["public_key"], received["signature"]) is True


async def test_run_reads_cached_token_from_default_path_when_flag_omitted(
    tmp_path, monkeypatch, fake_vllm_server
):
    """No --github-token-file given, but a token already exists at the
    default cache path (injected via default_github_token_path for
    testability, since the real default is ~/.mycelium/github-token) —
    must be read verbatim, and the device-flow client must never be
    called."""
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    default_token_path = tmp_path / "cached-github-token"
    default_token_path.write_text("gh-cached-token\n")

    class _ExplodingClient:
        def request_device_code(self):
            raise AssertionError("device flow must not run when a cached token exists")

        def poll_once(self, device_code, interval):
            raise AssertionError("device flow must not run when a cached token exists")

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
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(
                args, process,
                default_github_token_path=default_token_path,
                device_flow_client=_ExplodingClient(),
            )
        )
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert received["github_token"] == "gh-cached-token"


async def test_run_authenticates_and_caches_token_when_default_path_missing(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    default_token_path = tmp_path / "not-yet-created" / "github-token"

    device = github_device_flow.DeviceCode(
        device_code="devcode123", user_code="ABCD-1234",
        verification_uri="https://github.com/login/device",
        expires_in=900, interval=5,
    )
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token="gho_fresh", interval=5)]
    )

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
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(
                args, process,
                default_github_token_path=default_token_path,
                device_flow_client=client,
                device_flow_sleep=_no_op_sleep,
                device_flow_open_browser=_no_op_open_browser,
            )
        )
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert received["github_token"] == "gho_fresh"
    assert default_token_path.read_text() == "gho_fresh"
    assert oct(default_token_path.stat().st_mode)[-3:] == "600"


async def test_run_rejects_missing_explicit_github_token_file_before_starting_vllm(
    tmp_path, monkeypatch, fake_vllm_server
):
    """An explicitly-passed --github-token-file that doesn't exist is a
    hard error — never a fallback into the interactive device flow (see
    the design doc for issue #34)."""
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    missing_token_file = tmp_path / "does-not-exist"

    args = parse_args(
        [
            "--coordinator-url", "wss://127.0.0.1:1",
            "--coordinator-cert", str(cert_path),
            "--github-token-file", str(missing_token_file),
            "--vllm-port", str(vllm_port),
        ]
    )
    process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    with pytest.raises(SystemExit, match="does not exist"):
        await _run(args, process)

    # vLLM must never have been started — the check happens before
    # process.start(), so there's nothing to clean up here and no
    # subprocess was spawned.


async def test_run_prints_stale_token_hint_on_matching_rejection_reason(
    tmp_path, monkeypatch, fake_vllm_server, capsys
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    default_token_path = tmp_path / "github-token"
    default_token_path.write_text("stale-token")

    async def rejecting_coordinator(websocket):
        await websocket.recv()
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "invalid or expired GitHub token"}
        ))
        await websocket.close()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(rejecting_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(args, process, default_github_token_path=default_token_path)
        )
        await asyncio.sleep(1.5)  # let it attempt once and get rejected
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    out = capsys.readouterr().out
    assert "stale GitHub token" in out
    assert str(default_token_path) in out


async def test_run_prints_stale_token_hint_naming_the_explicit_token_file_when_one_was_passed(
    tmp_path, monkeypatch, fake_vllm_server, capsys
):
    """Regression test for the final-review finding: when --github-token-file
    is passed explicitly, the stale-token retry hint must name that file —
    not the unrelated default cache path, which was never even consulted."""
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    github_token_file = tmp_path / "explicit-github-token"
    github_token_file.write_text("stale-token")
    default_token_path = tmp_path / "unused-default-github-token"

    async def rejecting_coordinator(websocket):
        await websocket.recv()
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "invalid or expired GitHub token"}
        ))
        await websocket.close()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(rejecting_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--github-token-file", str(github_token_file),
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(args, process, default_github_token_path=default_token_path)
        )
        await asyncio.sleep(1.5)  # let it attempt once and get rejected
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    out = capsys.readouterr().out
    assert "stale GitHub token" in out
    assert str(github_token_file) in out
    assert str(default_token_path) not in out


async def test_run_answers_a_routed_complete_request(tmp_path, monkeypatch, fake_vllm_server):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    github_token_file = tmp_path / "github-token"
    github_token_file.write_text("gh-secret-token\n")

    reply_event = asyncio.Event()
    received_reply = {}

    async def fake_coordinator(websocket):
        await websocket.recv()  # registration
        await websocket.send(json.dumps({"type": "registered"}))
        await websocket.send(json.dumps(
            {"type": "complete", "request_id": "req-1", "prompt": "what is the answer?"}
        ))
        received_reply.update(json.loads(await websocket.recv()))
        reply_event.set()
        await websocket.wait_closed()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--github-token-file", str(github_token_file),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(_run(args, process))
        await asyncio.wait_for(reply_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert received_reply == {
        "type": "complete_result", "request_id": "req-1", "text": "fake completion",
    }


async def test_run_retries_after_registration_rejected(tmp_path, monkeypatch, fake_vllm_server):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    default_token_path = tmp_path / "github-token"
    default_token_path.write_text("gh-cached-token")

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
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(args, process, default_github_token_path=default_token_path)
        )
        await asyncio.sleep(2.5)  # let it attempt, get rejected, back off (~1s), attempt again
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert 2 <= attempt_count <= 4, (
        f"expected a small, backoff-bounded number of attempts, got {attempt_count} "
        "(a much higher count would indicate the no-backoff regression)"
    )


async def test_registration_backoff_resets_after_a_successful_registration(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    default_token_path = tmp_path / "github-token"
    default_token_path.write_text("gh-cached-token")

    attempt_times = []

    async def flaky_coordinator(websocket):
        attempt_times.append(time.monotonic())
        attempt_number = len(attempt_times)
        await websocket.recv()
        if attempt_number == 3:
            # Third attempt succeeds and immediately drops — this should
            # reset the registration backoff grown by attempts 1 and 2's
            # rejections (~1s then ~2s consumed).
            await websocket.send(json.dumps({"type": "registered"}))
            await websocket.close()
        else:
            # Attempts 1, 2, 4, 5: reject immediately.
            await websocket.send(json.dumps({"type": "registration_rejected", "reason": "bad"}))
            await websocket.close()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(flaky_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(args, process, default_github_token_path=default_token_path)
        )
        # Attempts 1-2 reject (growing the registration backoff to ~1s then
        # ~2s consumed), attempt 3 succeeds+drops (should reset the
        # backoff), attempts 4-5 reject again. If the reset didn't happen,
        # attempt 4's failure would consume the *next* step of the
        # already-grown generator (~3.2-4.8s) instead of the reset ~1s,
        # pushing attempt 5 past this window.
        await asyncio.sleep(6.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert len(attempt_times) >= 5, (
        f"expected at least 5 attempts within 6s if the backoff resets after "
        f"success, got {len(attempt_times)} attempts "
        "(a broken reset would carry the grown ~3.2-4.8s delay into attempt "
        "4's wait, pushing attempt 5 past this window)"
    )
    gap = attempt_times[4] - attempt_times[3]
    assert gap < 2.0, (
        f"expected the gap between attempt 4 and attempt 5 to reflect the "
        f"reset ~1s backoff, got {gap:.2f}s "
        "(a broken reset would carry over the ~3.2-4.8s delay grown by "
        "attempts 1-2 instead)"
    )


async def test_run_rejects_empty_github_token_file_before_starting_vllm(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    github_token_file = tmp_path / "github-token"
    github_token_file.write_text("  \n")

    args = parse_args(
        [
            "--coordinator-url", "wss://127.0.0.1:1",
            "--coordinator-cert", str(cert_path),
            "--github-token-file", str(github_token_file),
            "--vllm-port", str(vllm_port),
        ]
    )
    process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    with pytest.raises(SystemExit, match="empty"):
        await _run(args, process)

    # vLLM must never have been started — the empty-file check happens
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
    github_token_file = tmp_path / "github-token"
    github_token_file.write_text("gh-secret-token")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_VLLM_PID_DIR"] = str(pid_dir)
    vllm_port = _free_port()

    node_proc = subprocess.Popen(
        [
            sys.executable, "-m", "mycelium.node.cli",
            "--coordinator-url", "wss://127.0.0.1:1",
            "--coordinator-cert", str(cert_path),
            "--github-token-file", str(github_token_file),
            "--vllm-port", str(vllm_port),
            "--node-key-file", str(tmp_path / "node-key.pem"),
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


class _FakeDeviceFlowClient:
    """Scripted device-flow client for _authenticate tests — no real
    network calls, no real waiting.

    `device_or_devices` is either a single DeviceCode (returned from every
    request_device_code() call — the common case) or a list of DeviceCode
    (returned one per call, the last entry repeating once exhausted — only
    the expired-token test needs this, to prove a fresh code was actually
    requested and used).

    `poll_responses` is a list consumed one entry per poll_once() call;
    each entry is either a PollResult or an exception instance to raise.
    """

    def __init__(self, device_or_devices, poll_responses: list):
        devices = (
            device_or_devices if isinstance(device_or_devices, list) else [device_or_devices]
        )
        self._devices = list(devices)
        self.poll_responses = list(poll_responses)
        self.request_device_code_calls = 0
        self.poll_once_calls = []

    def request_device_code(self):
        self.request_device_code_calls += 1
        if len(self._devices) > 1:
            return self._devices.pop(0)
        return self._devices[0]

    def poll_once(self, device_code, interval):
        self.poll_once_calls.append((device_code, interval))
        response = self.poll_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


async def _no_op_sleep(seconds):
    pass


def _no_op_open_browser(url):
    """Fake for _authenticate's/_run's open_browser param — real tests
    must never let the real webbrowser.open run, since _device_code()'s
    default verification_uri is a real, live GitHub URL."""
    pass


def _device_code(**overrides):
    fields = dict(
        device_code="devcode123", user_code="ABCD-1234",
        verification_uri="https://github.com/login/device",
        expires_in=900, interval=5,
    )
    fields.update(overrides)
    return github_device_flow.DeviceCode(**fields)


async def test_authenticate_prints_code_and_returns_token_on_success(capsys):
    device = _device_code()
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token=None, interval=5),
                 github_device_flow.PollResult(token="gho_abc123", interval=5)]
    )

    token = await _authenticate(client=client, sleep=_no_op_sleep, open_browser=_no_op_open_browser)

    assert token == "gho_abc123"
    out = capsys.readouterr().out
    assert "ABCD-1234" in out
    assert "https://github.com/login/device" in out


async def test_authenticate_keeps_polling_through_authorization_pending():
    device = _device_code()
    client = _FakeDeviceFlowClient(
        device,
        [github_device_flow.PollResult(token=None, interval=5)] * 3
        + [github_device_flow.PollResult(token="gho_final", interval=5)],
    )

    token = await _authenticate(client=client, sleep=_no_op_sleep, open_browser=_no_op_open_browser)

    assert token == "gho_final"
    assert len(client.poll_once_calls) == 4


async def test_authenticate_uses_updated_interval_after_slow_down():
    device = _device_code(interval=5)
    client = _FakeDeviceFlowClient(
        device,
        [github_device_flow.PollResult(token=None, interval=12),
         github_device_flow.PollResult(token="gho_abc", interval=12)],
    )

    await _authenticate(client=client, sleep=_no_op_sleep, open_browser=_no_op_open_browser)

    # First poll uses the device's initial interval (5); the second poll
    # must use the interval slow_down returned (12), not the original 5.
    assert client.poll_once_calls[0][1] == 5
    assert client.poll_once_calls[1][1] == 12


async def test_authenticate_requests_a_fresh_code_after_expired_token(capsys):
    first_device = _device_code(user_code="OLD-CODE", device_code="old-devcode")
    second_device = _device_code(user_code="NEW-CODE", device_code="new-devcode")
    # Two devices in sequence: request_device_code() returns first_device
    # on the first call (consumed by the initial code print), then
    # second_device on the second call (the retry after DeviceCodeExpired)
    # — proving _authenticate actually requested and used a fresh code
    # rather than reusing the expired one.
    client = _FakeDeviceFlowClient(
        [first_device, second_device],
        [github_device_flow.DeviceCodeExpired(), github_device_flow.PollResult(token="gho_x", interval=5)],
    )

    token = await _authenticate(client=client, sleep=_no_op_sleep, open_browser=_no_op_open_browser)

    assert token == "gho_x"
    assert client.request_device_code_calls == 2
    assert client.poll_once_calls[-1][0] == "new-devcode"
    out = capsys.readouterr().out
    assert "OLD-CODE" in out
    assert "NEW-CODE" in out


async def test_authenticate_exits_on_authorization_denied():
    device = _device_code()
    client = _FakeDeviceFlowClient(device, [github_device_flow.AuthorizationDenied()])

    with pytest.raises(SystemExit, match="denied"):
        await _authenticate(client=client, sleep=_no_op_sleep, open_browser=_no_op_open_browser)


async def test_authenticate_exits_on_device_flow_config_error_from_poll():
    device = _device_code()
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.DeviceFlowConfigError("unexpected response from GitHub")]
    )

    with pytest.raises(SystemExit, match="unexpected response"):
        await _authenticate(client=client, sleep=_no_op_sleep, open_browser=_no_op_open_browser)


async def test_authenticate_exits_on_device_flow_config_error_from_initial_request():
    class _FailingClient:
        def request_device_code(self):
            raise github_device_flow.DeviceFlowConfigError("not configured yet")

    with pytest.raises(SystemExit, match="not configured"):
        await _authenticate(client=_FailingClient(), sleep=_no_op_sleep, open_browser=_no_op_open_browser)


async def test_authenticate_opens_browser_to_verification_uri():
    opened = []

    device = _device_code(verification_uri="https://github.com/login/device")
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token="gho_abc", interval=5)]
    )

    await _authenticate(client=client, sleep=_no_op_sleep, open_browser=opened.append)

    assert opened == ["https://github.com/login/device"]


async def test_authenticate_swallows_browser_open_failures(capsys):
    def raising_open(url):
        raise RuntimeError("no display available")

    device = _device_code()
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token="gho_abc", interval=5)]
    )

    # Must not raise — a headless box without a display must never see an
    # error from the best-effort browser-open convenience.
    token = await _authenticate(client=client, sleep=_no_op_sleep, open_browser=raising_open)

    assert token == "gho_abc"


async def test_authenticate_open_browser_default_resolves_webbrowser_open_at_call_time(monkeypatch):
    """Regression test for the final-review finding: open_browser's default
    must be resolved fresh on every call (`open_browser or webbrowser.open`),
    not bound once to the real function at import time — otherwise a
    monkeypatch.setattr(webbrowser, "open", ...) applied after cli.py was
    imported (i.e. in every test) would silently have no effect on calls
    that fall through to the default, which is exactly what defeats the
    tests/conftest.py safety net for this same incident."""
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    device = _device_code(verification_uri="https://github.com/login/device")
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token="gho_abc", interval=5)]
    )

    # No open_browser passed — relies entirely on the default.
    await _authenticate(client=client, sleep=_no_op_sleep)

    assert opened == ["https://github.com/login/device"]
