"""Starts, monitors, and stops the local vLLM server process, and forwards
prompts to it over HTTP.

See the design doc for issue #7. This module owns the subprocess
lifecycle of `vllm serve` and the local forwarding call — it has no
awareness of the coordinator connection (mycelium.node.connection).
"""

from __future__ import annotations

import json
import os
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
