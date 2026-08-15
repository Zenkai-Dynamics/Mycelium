# Issue #7 — Node Agent Wraps and Manages the Local vLLM Process — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The node agent (`mycelium-node`) automatically starts a local `vllm serve` subprocess on startup, forwards a prompt to it locally, and stops it cleanly (no orphaned GPU processes) on shutdown.

**Architecture:** A new module, `mycelium/node/vllm_process.py`, owns the `vllm serve` subprocess lifecycle (build the command/env, start it, poll `/health` until ready, forward a prompt to `/v1/chat/completions`, stop it via a process-group kill). `mycelium/node/cli.py` wires this into `mycelium-node`'s startup/shutdown: it starts vLLM before doing anything else, then either runs a new one-shot `--prompt` mode (send one prompt, print the result, stop vLLM, exit — the live-verification path) or the existing coordinator connect loop, stopping vLLM in a `finally` block on every exit path. This plan covers only the code and tests in this repo, which don't require a GPU. **Real-hardware verification against `vllm serve` actually running (on the `a6000` node) is performed separately, directly by the operator/orchestrating agent after this plan's tasks are done** — not a task here, for the same reason issues #4/#5/#6's live-hardware steps weren't delegated to a subagent: it needs live SSH access, a real GPU, and real-time debugging judgment. See the design doc's "Live verification" note.

**Tech Stack:** Python 3.11+, stdlib only (`subprocess`, `urllib.request`, `http.server`, `asyncio`, `os`, `signal`, `json`). No new dependencies.

## Global Constraints

- No new runtime dependencies — the local HTTP call to vLLM uses stdlib `urllib.request` wrapped in `asyncio.to_thread`, not `httpx`/`aiohttp` (design doc: "HTTP client" decision).
- `VLLM_USE_FLASHINFER_SAMPLER=0` is always injected into the `vllm serve` subprocess's environment, unconditionally — not a CLI flag (design doc: known `flashinfer`/CCCL packaging issue from issue #6).
- Defaults: `--model` = `Qwen/Qwen2.5-7B-Instruct`, `--gpu` = `"0"`, `--vllm-port` = `8811` — all overridable via CLI flags, never hardcoded constants with no escape hatch.
- `HF_HOME` is never set or overridden by the node agent — the `vllm serve` subprocess inherits the parent process's environment unchanged, aside from `CUDA_VISIBLE_DEVICES` and `VLLM_USE_FLASHINFER_SAMPLER` (design doc: "`HF_HOME` is not managed by the node agent").
- Stopping vLLM must kill its whole process group (`subprocess.Popen(..., start_new_session=True)` + `os.killpg`, `SIGTERM` then `SIGKILL` after a timeout) — a bare PID kill is insufficient and risks orphaning `vllm serve`'s own worker subprocesses (design doc: "Process lifecycle" decision).
- `--coordinator-url`/`--coordinator-cert` become optional on `mycelium-node`; `parse_args` must require at least one of `{--prompt, both coordinator args together}` (design doc: "Coordinator arguments become optional" decision).
- Match existing module style: `from __future__ import annotations`, a module docstring explaining scope/boundaries (see `mycelium/node/connection.py`, `mycelium/coordinator/server.py`), `SCREAMING_SNAKE_CASE` module-level constants for tunables.

---

### Task 1: `vllm serve` command and environment construction

**Files:**
- Create: `src/mycelium/node/vllm_process.py`
- Test: `tests/node/test_vllm_process.py`

**Interfaces:**
- Consumes: nothing from other tasks — first task.
- Produces: `build_command(model: str, port: int) -> list[str]`, `build_env(gpu: str) -> dict[str, str]`, module constants `DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"`, `DEFAULT_GPU = "0"`, `DEFAULT_PORT = 8811` — all consumed by Task 2 and Task 4.

- [ ] **Step 1: Write the failing tests**

Create `tests/node/test_vllm_process.py`:

```python
"""Tests for mycelium.node.vllm_process."""

from mycelium.node import vllm_process


def test_build_command():
    command = vllm_process.build_command("Qwen/Qwen2.5-7B-Instruct", 8811)
    assert command == ["vllm", "serve", "Qwen/Qwen2.5-7B-Instruct", "--port", "8811"]


def test_build_env_sets_gpu_pin_and_flashinfer_flag(monkeypatch):
    monkeypatch.setenv("SOME_EXISTING_VAR", "keep-me")
    env = vllm_process.build_env("2")
    assert env["CUDA_VISIBLE_DEVICES"] == "2"
    assert env["VLLM_USE_FLASHINFER_SAMPLER"] == "0"
    assert env["SOME_EXISTING_VAR"] == "keep-me"  # parent env preserved, not replaced
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_vllm_process.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.node.vllm_process'`

- [ ] **Step 3: Write the implementation**

Create `src/mycelium/node/vllm_process.py`:

```python
"""Starts, monitors, and stops the local vLLM server process, and forwards
prompts to it over HTTP.

See the design doc for issue #7. This module owns the subprocess
lifecycle of `vllm serve` and the local forwarding call — it has no
awareness of the coordinator connection (mycelium.node.connection).
"""

from __future__ import annotations

import os

DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_GPU = "0"
DEFAULT_PORT = 8811


def build_command(model: str, port: int) -> list[str]:
    """Build the `vllm serve` argv — the same invocation validated in issue #6."""
    return ["vllm", "serve", model, "--port", str(port)]


def build_env(gpu: str) -> dict[str, str]:
    """Build the subprocess environment: parent env plus the GPU pin and the
    flashinfer-sampler workaround (see issue #6's design doc). HF_HOME is
    deliberately left untouched — the operator sets it, same as issue #6 did.
    """
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = gpu
    env["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
    return env
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_vllm_process.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/vllm_process.py tests/node/test_vllm_process.py
git commit -m "feat: build vllm serve command and environment"
```

---

### Task 2: Readiness polling and prompt forwarding

**Files:**
- Modify: `src/mycelium/node/vllm_process.py`
- Test: `tests/node/test_vllm_process.py`

**Interfaces:**
- Consumes: `DEFAULT_MODEL`, `DEFAULT_GPU`, `DEFAULT_PORT` from Task 1.
- Produces: `class VLLMProcess(model=DEFAULT_MODEL, gpu=DEFAULT_GPU, port=DEFAULT_PORT)` with `.wait_ready(timeout: float = READY_TIMEOUT_SECONDS) -> None` (raises `VLLMReadyTimeout` on timeout) and `.complete(prompt: str, timeout: float = COMPLETE_TIMEOUT_SECONDS) -> str`. `VLLMReadyTimeout` exception. Consumed by Task 3 (adds `.start`/`.stop` to the same class) and Task 4 (`cli.py`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/node/test_vllm_process.py`:

```python
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from mycelium.node.vllm_process import VLLMProcess, VLLMReadyTimeout


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
    process = VLLMProcess(port=39999)  # nothing listening on this port
    with pytest.raises(VLLMReadyTimeout):
        process.wait_ready(timeout=1.0)


def test_complete_returns_completion_content(fake_vllm_server):
    port = fake_vllm_server.server_address[1]
    process = VLLMProcess(port=port)
    result = process.complete("What is the answer?")
    assert result == "the answer is 42"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_vllm_process.py -v`
Expected: FAIL with `ImportError: cannot import name 'VLLMProcess' from 'mycelium.node.vllm_process'`

- [ ] **Step 3: Write the implementation**

Add to `src/mycelium/node/vllm_process.py` (after the `build_env` function):

```python
import json
import time
import urllib.error
import urllib.request

HEALTH_POLL_INTERVAL_SECONDS = 1.0
HEALTH_REQUEST_TIMEOUT_SECONDS = 2.0
READY_TIMEOUT_SECONDS = 300.0  # vLLM model load can take a while
COMPLETE_TIMEOUT_SECONDS = 120.0


class VLLMReadyTimeout(Exception):
    """Raised when vLLM doesn't become healthy within the timeout."""


class VLLMProcess:
    """Manages one `vllm serve` subprocess and forwards prompts to it."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        gpu: str = DEFAULT_GPU,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.model = model
        self.gpu = gpu
        self.port = port

    def wait_ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        """Poll /health until vLLM responds 200, or raise VLLMReadyTimeout."""
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.port}/health"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=HEALTH_REQUEST_TIMEOUT_SECONDS) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
        raise VLLMReadyTimeout(f"vLLM did not become healthy within {timeout}s")

    def complete(self, prompt: str, timeout: float = COMPLETE_TIMEOUT_SECONDS) -> str:
        """Forward a prompt to vLLM's OpenAI-compatible chat endpoint, return the completion text."""
        url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        payload = json.dumps(
            {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        ).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return body["choices"][0]["message"]["content"]
```

Move the `import os` line and the two `DEFAULT_*` constant blocks so the file reads top-to-bottom as: module docstring, `from __future__ import annotations`, stdlib imports (`os`, `json`, `time`, `urllib.error`, `urllib.request`), then constants, then `build_command`/`build_env`, then `VLLMReadyTimeout`, then `VLLMProcess`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_vllm_process.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/vllm_process.py tests/node/test_vllm_process.py
git commit -m "feat: vLLM readiness polling and prompt forwarding"
```

---

### Task 3: Process lifecycle — start/stop with no orphaned processes

**Files:**
- Modify: `src/mycelium/node/vllm_process.py`
- Create: `tests/node/fixtures/fake_vllm.py`
- Test: `tests/node/test_vllm_process.py`

**Interfaces:**
- Consumes: `VLLMProcess`, `build_command`, `build_env` from Tasks 1-2.
- Produces: `VLLMProcess.start(command: list[str] | None = None) -> None` and `VLLMProcess.stop(timeout: float = STOP_TIMEOUT_SECONDS) -> None`. Consumed by Task 4 (`cli.py`).

- [ ] **Step 1: Create the subprocess fixture used by the orphan test**

Create `tests/node/fixtures/fake_vllm.py` (a standalone script — not a pytest file — that stands in for `vllm serve`: it spawns its own child process, the way vLLM spawns engine-core worker processes, so the test can prove a process-group kill takes both down):

```python
"""Stand-in for `vllm serve` used only by
tests/node/test_vllm_process.py's process-group-kill test. Spawns a child
process (mimicking vLLM's own worker subprocess) and serves the same
/health and /v1/chat/completions surface the real fake server fixture in
test_vllm_process.py implements, so this script can be driven through
VLLMProcess exactly like the real thing.
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
pid_file = sys.argv[2]
child_pid_file = sys.argv[3]

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])

with open(pid_file, "w") as f:
    f.write(str(os.getpid()))
with open(child_pid_file, "w") as f:
    f.write(str(child.pid))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
```

- [ ] **Step 2: Write the failing test**

Add to `tests/node/test_vllm_process.py`:

```python
import os
import sys
import time
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_start_stop_kills_process_group_with_no_orphans(tmp_path):
    pid_file = tmp_path / "pid"
    child_pid_file = tmp_path / "child_pid"
    port = 39998
    command = [
        sys.executable,
        str(FIXTURES_DIR / "fake_vllm.py"),
        str(port),
        str(pid_file),
        str(child_pid_file),
    ]

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

    time.sleep(0.5)  # give the OS a moment to reap the killed processes
    assert not _process_alive(parent_pid)
    assert not _process_alive(child_pid)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/node/test_vllm_process.py::test_start_stop_kills_process_group_with_no_orphans -v`
Expected: FAIL with `AttributeError: 'VLLMProcess' object has no attribute 'start'`

- [ ] **Step 4: Write the implementation**

Add to `src/mycelium/node/vllm_process.py`'s imports: `import signal`, `import subprocess`. Add the new constant near the other timeout constants:

```python
STOP_TIMEOUT_SECONDS = 15.0
```

Add to `VLLMProcess.__init__`, right after the existing three assignments:

```python
        self._process: subprocess.Popen | None = None
```

Add these two methods to `VLLMProcess` (after `__init__`, before `wait_ready`):

```python
    def start(self, command: list[str] | None = None) -> None:
        """Launch `vllm serve` in its own process group (so stop() can kill
        its worker subprocesses too, not just this one PID)."""
        self._process = subprocess.Popen(
            command or build_command(self.model, self.port),
            env=build_env(self.gpu),
            start_new_session=True,
        )

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """SIGTERM the whole process group, escalating to SIGKILL if it
        doesn't exit in time. No-op if start() was never called or the
        process already exited."""
        if self._process is None or self._process.poll() is not None:
            return
        pgid = os.getpgid(self._process.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            self._process.wait()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/node/test_vllm_process.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the full test file once more to confirm nothing else broke**

Run: `pytest tests/node/test_vllm_process.py -v`
Expected: PASS (6 tests, ~10-15s total — the orphan test waits on real subprocess start/stop timing)

- [ ] **Step 7: Commit**

```bash
git add src/mycelium/node/vllm_process.py tests/node/test_vllm_process.py tests/node/fixtures/fake_vllm.py
git commit -m "feat: vLLM process start/stop with process-group kill"
```

---

### Task 4: Wire vLLM lifecycle into the `mycelium-node` CLI

**Files:**
- Modify: `src/mycelium/node/cli.py`
- Modify: `tests/node/test_cli.py`

**Interfaces:**
- Consumes: `VLLMProcess`, `DEFAULT_MODEL`, `DEFAULT_GPU`, `DEFAULT_PORT` from `mycelium.node.vllm_process` (Tasks 1-3); `connection.connect` (existing, unchanged).
- Produces: nothing consumed by other tasks — last task in this plan.

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `tests/node/test_cli.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_cli.py -v`
Expected: 5 of 7 FAIL, 2 incidentally PASS already:
- `test_parse_args_partial_coordinator_args_rejected` and `test_parse_args_requires_coordinator_or_prompt` **pass already** — today's `parse_args` already marks both coordinator args `required=True`, so `parse_args([])` and either-arg-alone both already raise `SystemExit`, coincidentally satisfying these two before any new code is written.
- `test_parse_args_prompt_alone_is_valid`, `test_parse_args_defaults`, `test_parse_args_overrides` **fail** with `SystemExit`/`error: unrecognized arguments: --prompt` (or `--model`/`--gpu`/`--vllm-port`) — those flags don't exist yet.
- `test_parse_args_coordinator_alone_is_valid` **fails** with `AttributeError: 'Namespace' object has no attribute 'prompt'` — valid args parse today, but `--prompt` isn't a recognized attribute yet.
- `test_run_prompt_mode_forwards_prompt_and_prints_completion` **fails** with `SystemExit` from the unrecognized `--prompt` flag before `_run` is ever reached.

- [ ] **Step 3: Write the implementation**

Replace the full contents of `src/mycelium/node/cli.py`:

```python
"""CLI entry point for the Mycelium node agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from mycelium import __version__
from mycelium.node import connection
from mycelium.node.vllm_process import (
    DEFAULT_GPU,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    VLLMProcess,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-node")
    parser.add_argument("--coordinator-url", default=None)
    parser.add_argument("--coordinator-cert", type=Path, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gpu", default=DEFAULT_GPU)
    parser.add_argument("--vllm-port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--prompt",
        default=None,
        help="Send this one prompt to vLLM and exit, without connecting to a coordinator.",
    )
    args = parser.parse_args(argv)

    has_url = args.coordinator_url is not None
    has_cert = args.coordinator_cert is not None
    if has_url != has_cert:
        parser.error("--coordinator-url and --coordinator-cert must be given together")
    if not (has_url and has_cert) and args.prompt is None:
        parser.error("either --coordinator-url/--coordinator-cert or --prompt is required")

    return args


async def _run(args: argparse.Namespace) -> None:
    process = VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
    print(f"starting vLLM ({args.model} on GPU {args.gpu})...", flush=True)
    await asyncio.to_thread(process.start)
    try:
        await asyncio.to_thread(process.wait_ready)
        print("vLLM ready", flush=True)

        if args.prompt is not None:
            result = await asyncio.to_thread(process.complete, args.prompt)
            print(result, flush=True)
            return

        print(f"mycelium-node {__version__} connecting to {args.coordinator_url}", flush=True)
        async for websocket in connection.connect(args.coordinator_url, args.coordinator_cert):
            print(f"connected to coordinator ({args.coordinator_url})", flush=True)
            try:
                await websocket.wait_closed()
                print("connection to coordinator closed, reconnecting...", flush=True)
            except Exception:
                continue
    finally:
        await asyncio.to_thread(process.stop)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_cli.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS, all tests across `tests/coordinator/`, `tests/node/`, and `tests/test_integration.py` — confirm nothing in `connection.py`/`server.py`/`certs.py` broke.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/node/cli.py tests/node/test_cli.py
git commit -m "feat: node agent starts, forwards to, and stops vLLM"
```

---

## After this plan: real-hardware verification (not a subagent task)

Once all four tasks are merged, the orchestrating agent runs `mycelium-node --prompt "..."` for real against the `a6000` node (`training-framework@192.168.22.23`, already in the operator's SSH config — the same machine issue #6 verified against), confirms a correct completion, and confirms `nvidia-smi` shows no leftover vLLM process after the command exits. That narrative gets added to `docs/superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md` (a new "Live verification" section, following issue #6's precedent), and `docs/phases/phase-0-foundation.md`'s `VLLM_USE_FLASHINFER_SAMPLER=0` open-risk bullet gets updated to note it's now implemented in code, not just a documented requirement.
