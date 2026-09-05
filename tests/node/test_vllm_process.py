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

RECEIVED_BODIES: list[dict] = []

# Set by a test to make the next /v1/chat/completions call fail with a
# chosen status and body; cleared by the fake_vllm_server fixture. A dict
# rather than a module-level rebind so the handler can read it without a
# `global` declaration, matching RECEIVED_BODIES above.
RESPONSE_OVERRIDE: dict = {}


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
            RECEIVED_BODIES.append(json.loads(self.rfile.read(length)))
            if RESPONSE_OVERRIDE:
                body = RESPONSE_OVERRIDE["body"]
                self.send_response(RESPONSE_OVERRIDE["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
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
    RECEIVED_BODIES.clear()
    RESPONSE_OVERRIDE.clear()
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
    result = process.complete([{"role": "user", "content": "What is the answer?"}])
    assert result == "the answer is 42"


def test_complete_sends_the_messages_array_through_unchanged(fake_vllm_server):
    RECEIVED_BODIES.clear()
    port = fake_vllm_server.server_address[1]
    process = VLLMProcess(model="test-model", port=port)

    messages = [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "and of Spain?"},
    ]
    text = process.complete(messages)

    assert text == "the answer is 42"
    assert RECEIVED_BODIES[0]["messages"] == messages, (
        "roles must survive to vLLM intact, not be flattened into one user message"
    )
    assert RECEIVED_BODIES[0]["model"] == "test-model"


def _overflow_body() -> bytes:
    """The shape vLLM actually returns for a context-window overflow —
    `message` at the top level, not nested under `error` as some
    OpenAI-compatible servers do."""
    return json.dumps({
        "object": "error",
        "message": "This model's maximum context length is 32768 tokens, "
                   "however you requested 41022 tokens",
        "type": "BadRequestError",
        "code": 400,
    }).encode()


def test_context_overflow_raises_client_error_with_vllm_message(fake_vllm_server):
    RESPONSE_OVERRIDE.update({"status": 400, "body": _overflow_body()})
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMClientError) as exc:
        process.complete([{"role": "user", "content": "hi"}])

    assert "maximum context length" in str(exc.value)
    assert "41022" in str(exc.value), (
        "the requested size is what makes an overflow actionable — it must survive"
    )


def test_model_not_found_is_a_node_fault_despite_being_4xx(fake_vllm_server):
    """404 means the node registered a model its vLLM isn't serving."""
    RESPONSE_OVERRIDE.update({
        "status": 404,
        "body": json.dumps({"message": "The model `other` does not exist"}).encode(),
    })
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMServerError):
        process.complete([{"role": "user", "content": "hi"}])


def test_overloaded_statuses_are_node_faults(fake_vllm_server):
    """408 and 429 are the node's capacity, not a defect in the request."""
    for status in (408, 429):
        RESPONSE_OVERRIDE.clear()
        RESPONSE_OVERRIDE.update({
            "status": status, "body": json.dumps({"message": "busy"}).encode(),
        })
        process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

        with pytest.raises(vllm_process.VLLMServerError):
            process.complete([{"role": "user", "content": "hi"}])


def test_auth_and_entity_too_large_statuses_are_node_faults(fake_vllm_server):
    """401, 403 and 413 are the volunteer's own auth/proxy configuration,
    not a defect in the request — the same reasoning that carved out
    404/408/429. See the design doc for issue #58."""
    for status in (401, 403, 413):
        RESPONSE_OVERRIDE.clear()
        RESPONSE_OVERRIDE.update({
            "status": status, "body": json.dumps({"message": "nope"}).encode(),
        })
        process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

        with pytest.raises(vllm_process.VLLMServerError):
            process.complete([{"role": "user", "content": "hi"}])


def test_server_error_is_a_node_fault(fake_vllm_server):
    RESPONSE_OVERRIDE.update({
        "status": 500, "body": json.dumps({"message": "engine died"}).encode(),
    })
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMServerError) as exc:
        process.complete([{"role": "user", "content": "hi"}])

    assert "500" in str(exc.value)
    assert "engine died" not in str(exc.value), (
        "a node-fault message must not relay vLLM's error body — vLLM and "
        "torch exception strings routinely embed filesystem paths under "
        "the volunteer's home directory. See the design doc for issue #58."
    )


def test_unparseable_error_body_falls_back_to_the_raw_text(fake_vllm_server):
    """An unexpected error shape must still produce something actionable."""
    RESPONSE_OVERRIDE.update({"status": 400, "body": b"<html>Bad Request</html>"})
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMClientError) as exc:
        process.complete([{"role": "user", "content": "hi"}])

    assert "Bad Request" in str(exc.value)


def test_nested_error_message_shape_is_also_read(fake_vllm_server):
    RESPONSE_OVERRIDE.update({
        "status": 400,
        "body": json.dumps({"error": {"message": "too many tokens"}}).encode(),
    })
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMClientError) as exc:
        process.complete([{"role": "user", "content": "hi"}])

    assert "too many tokens" in str(exc.value)


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
