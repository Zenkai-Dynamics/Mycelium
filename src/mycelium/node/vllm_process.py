"""Starts, monitors, and stops the local vLLM server process, and forwards
prompts to it over HTTP.

See the design doc for issue #7. This module owns the subprocess
lifecycle of `vllm serve` and the local forwarding call — it has no
awareness of the coordinator connection (mycelium.node.connection).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_GPU = "0"
DEFAULT_PORT = 8811

HEALTH_POLL_INTERVAL_SECONDS = 1.0
HEALTH_REQUEST_TIMEOUT_SECONDS = 2.0
READY_TIMEOUT_SECONDS = 300.0  # vLLM model load can take a while
COMPLETE_TIMEOUT_SECONDS = 120.0
STOP_TIMEOUT_SECONDS = 15.0


def build_command(model: str, port: int) -> list[str]:
    """Build the `vllm serve` argv — the same invocation validated in issue
    #6, plus an explicit loopback bind: nothing in this codebase ever talks
    to vLLM over anything but 127.0.0.1, and an independently-owned node
    shouldn't default to exposing it on every interface."""
    return ["vllm", "serve", model, "--host", "127.0.0.1", "--port", str(port)]


def build_env(gpu: str) -> dict[str, str]:
    """Build the subprocess environment: parent env plus the GPU pin and the
    flashinfer-sampler workaround (see issue #6's design doc). HF_HOME is
    deliberately left untouched — the operator sets it, same as issue #6 did.
    """
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = gpu
    env["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
    return env


class VLLMReadyTimeout(Exception):
    """Raised when vLLM doesn't become healthy within the timeout, or exits
    before becoming healthy."""


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
        self._process: subprocess.Popen | None = None
        self._pgid: int | None = None

    def start(self, command: list[str] | None = None) -> None:
        """Launch `vllm serve` in its own process group (so stop() can kill
        its worker subprocesses too, not just this one PID)."""
        self._process = subprocess.Popen(
            command or build_command(self.model, self.port),
            env=build_env(self.gpu),
            start_new_session=True,
        )
        # Captured now, not re-derived later: once the process exits, its
        # PID can be recycled by the OS, making a later os.getpgid(pid)
        # call unsafe/meaningless.
        self._pgid = os.getpgid(self._process.pid)

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """SIGTERM the whole process group, escalating to SIGKILL if it
        doesn't exit in time. No-op if start() was never called.

        Always attempts the process-group kill, even if vLLM's own leader
        process has already exited on its own — vLLM's worker subprocess(es)
        are a separate PID under the same group, and a leader-only exit
        must not leave them orphaned (the whole point of process-group
        cleanup instead of a bare PID kill).
        """
        if self._process is None:
            return
        try:
            os.killpg(self._pgid, signal.SIGTERM)
        except ProcessLookupError:
            return  # whole group already gone
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self._pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._process.wait()

    def wait_ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        """Poll /health until vLLM responds 200, or raise VLLMReadyTimeout —
        either because the timeout elapsed, or because vLLM exited early
        (crash, OOM, bad model id) and would otherwise poll a dead port for
        the full timeout before reporting a misleading error."""
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.port}/health"
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise VLLMReadyTimeout(
                    f"vLLM exited with code {self._process.returncode} "
                    "before becoming healthy"
                )
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
