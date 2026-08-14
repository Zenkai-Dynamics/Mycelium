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
