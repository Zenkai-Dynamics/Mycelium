"""Tests for mycelium.node.cli."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

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


def test_parse_args_coordinator_alone_is_valid():
    args = parse_args(
        ["--coordinator-url", "wss://example:8765", "--coordinator-cert", "/tmp/cert.pem"]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == "/tmp/cert.pem"
    assert args.prompt is None


def test_parse_args_defaults():
    args = parse_args(["--prompt", "hi"])
    assert args.model == vllm_process.DEFAULT_MODEL
    assert args.gpu == vllm_process.DEFAULT_GPU
    assert args.vllm_port == vllm_process.DEFAULT_PORT


def test_parse_args_overrides():
    args = parse_args(
        ["--prompt", "hi", "--model", "some/other-model", "--gpu", "1", "--vllm-port", "9000"]
    )
    assert args.model == "some/other-model"
    assert args.gpu == "1"
    assert args.vllm_port == 9000


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


async def test_run_prompt_mode_forwards_prompt_and_prints_completion(
    monkeypatch, capsys, fake_vllm_server
):
    port = fake_vllm_server.server_address[1]
    # Point the CLI's vLLM launch at the already-running fake server instead
    # of a real `vllm serve`, by making build_command exec a no-op stub.
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    args = parse_args(["--prompt", "what is the answer?", "--vllm-port", str(port)])

    await _run(args)

    assert "fake completion" in capsys.readouterr().out
