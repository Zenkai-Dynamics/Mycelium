"""Tests for mycelium.node.vllm_process."""

import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from mycelium.node import vllm_process
from mycelium.node.vllm_process import VLLMProcess, VLLMReadyTimeout

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_build_command():
    command = vllm_process.build_command("Qwen/Qwen2.5-7B-Instruct", 8811)
    assert command == [
        "vllm", "serve", "Qwen/Qwen2.5-7B-Instruct", "--host", "127.0.0.1", "--port", "8811",
    ]


def test_build_env_sets_gpu_pin_and_flashinfer_flag(monkeypatch):
    monkeypatch.setenv("SOME_EXISTING_VAR", "keep-me")
    env = vllm_process.build_env("2")
    assert env["CUDA_VISIBLE_DEVICES"] == "2"
    assert env["VLLM_USE_FLASHINFER_SAMPLER"] == "0"
    assert env["SOME_EXISTING_VAR"] == "keep-me"  # parent env preserved, not replaced


class _FakeVLLMHandler(BaseHTTPRequestHandler):
    """Just enough of vLLM's OpenAI-compatible surface for these tests."""

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
                {"choices": [{"message": {"content": "the answer is 42"}}]}
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
        pass  # quiet test output


@pytest.fixture
def fake_vllm_server():
    server = HTTPServer(("127.0.0.1", 0), _FakeVLLMHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


def test_wait_ready_returns_once_health_endpoint_is_up(fake_vllm_server):
    port = fake_vllm_server.server_address[1]
    process = VLLMProcess(port=port)
    process.wait_ready(timeout=5.0)  # should not raise


def test_wait_ready_raises_on_timeout():
    process = VLLMProcess(port=_free_port())  # nothing listening on this port
    with pytest.raises(VLLMReadyTimeout):
        process.wait_ready(timeout=1.0)


def test_complete_returns_completion_content(fake_vllm_server):
    port = fake_vllm_server.server_address[1]
    process = VLLMProcess(port=port)
    result = process.complete("What is the answer?")
    assert result == "the answer is 42"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_start_stop_kills_process_group_with_no_orphans(tmp_path):
    pid_file = tmp_path / "pid"
    child_pid_file = tmp_path / "child_pid"
    port = _free_port()
    command = [
        sys.executable,
        str(FIXTURES_DIR / "fake_vllm.py"),
        str(port),
        str(pid_file),
        str(child_pid_file),
    ]

    parent_pid = None
    child_pid = None
    process = VLLMProcess(port=port)
    process.start(command=command)
    try:
        process.wait_ready(timeout=10.0)
        parent_pid = int(pid_file.read_text())
        child_pid = int(child_pid_file.read_text())
        assert _process_alive(parent_pid)
        assert _process_alive(child_pid)
    finally:
        process.stop()

    assert parent_pid is not None and child_pid is not None, "vLLM never reported its PIDs"
    time.sleep(0.5)  # give the OS a moment to reap the killed processes
    assert not _process_alive(parent_pid)
    assert not _process_alive(child_pid)
