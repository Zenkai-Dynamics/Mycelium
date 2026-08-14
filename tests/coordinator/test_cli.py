"""Tests for mycelium.coordinator.cli."""

import pytest

from mycelium.coordinator.cli import parse_args, _run


def test_parse_args_defaults():
    args = parse_args([])
    assert args.host == "0.0.0.0"
    assert args.port == 8765


def test_parse_args_overrides():
    args = parse_args(["--host", "127.0.0.1", "--port", "9000"])
    assert args.host == "127.0.0.1"
    assert args.port == 9000


async def test_run_requires_cert_san_ip_when_no_existing_cert(tmp_path):
    args = parse_args(
        [
            "--cert-file", str(tmp_path / "cert.pem"),
            "--key-file", str(tmp_path / "key.pem"),
        ]
    )
    with pytest.raises(SystemExit):
        await _run(args)
