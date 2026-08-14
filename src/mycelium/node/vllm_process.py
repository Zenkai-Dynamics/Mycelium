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
