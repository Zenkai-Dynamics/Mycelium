"""Starts, monitors, and stops the local vLLM server process, and forwards
a messages array to it over HTTP.

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


# 4xx codes that are the NODE's fault despite being client-error codes:
# 404 means vLLM isn't serving the model this node registered (a
# misconfiguration), and 408/429 mean the node is too busy to serve.
# Classifying these as client faults would shield a node that genuinely
# cannot serve from ever reflecting it in its reputation — the mirror
# image of the bug issue #58 exists to fix. See the design doc for #58.
NODE_FAULT_STATUSES = frozenset({404, 408, 429})


class VLLMClientError(Exception):
    """vLLM rejected the request itself — most often context exceeding the
    model's window. The caller's mistake, not the node's, so it must never
    be recorded against this node's reputation. See the design doc for
    issue #58."""


class VLLMServerError(Exception):
    """vLLM failed for a reason that is the node's own — a 5xx, or one of
    NODE_FAULT_STATUSES. Recorded against reputation exactly as any other
    node failure."""


def _error_message(body: bytes, status: int) -> str:
    """Pull the human-readable message out of vLLM's error body.

    vLLM returns `message` at the top level; some OpenAI-compatible
    servers nest it under `error`. Both are read, and anything
    unparseable falls back to the raw text — an unexpected error shape
    must still produce something a caller can act on rather than an empty
    string. See the design doc for issue #58.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        message = parsed.get("message")
        if isinstance(message, str) and message:
            return message
        error = parsed.get("error")
        if isinstance(error, dict):
            nested = error.get("message")
            if isinstance(nested, str) and nested:
                return nested
    text = body.decode("utf-8", errors="replace").strip()
    return text or f"vLLM returned HTTP {status} with no message"


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

    def complete(self, messages: list[dict], timeout: float = COMPLETE_TIMEOUT_SECONDS) -> str:
        """Forward a conversation to vLLM's OpenAI-compatible chat endpoint,
        return the completion text.

        Takes the `messages` array as-is rather than a bare prompt string:
        as of issue #55 the coordinator is the single place that expands
        the one-message `prompt` shorthand, so by the time a request
        reaches a node it always carries real roles. This method no longer
        constructs any part of the conversation itself.
        """
        url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        payload = json.dumps({"model": self.model, "messages": messages}).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # Only HTTP responses are classified here. Transport failures
            # (URLError, socket timeouts) propagate unchanged and are
            # treated as node faults by request_handler's catch-all —
            # there is no client-caused way to make the local loopback
            # connection to vLLM fail. See the design doc for issue #58.
            message = _error_message(exc.read(), exc.code)
            if 400 <= exc.code < 500 and exc.code not in NODE_FAULT_STATUSES:
                raise VLLMClientError(message) from exc
            raise VLLMServerError(message) from exc
        return body["choices"][0]["message"]["content"]
