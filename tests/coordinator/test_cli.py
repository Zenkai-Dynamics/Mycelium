"""Tests for mycelium.coordinator.cli."""

import pytest

from mycelium.coordinator import certs
from mycelium.coordinator.cli import parse_args, _run


def test_parse_args_defaults(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    args = parse_args(["--token-file", str(token_file)])
    assert args.host == "0.0.0.0"
    assert args.port == 8765


def test_parse_args_overrides(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    args = parse_args(
        ["--host", "127.0.0.1", "--port", "9000", "--token-file", str(token_file)]
    )
    assert args.host == "127.0.0.1"
    assert args.port == 9000


def test_parse_args_requires_token_file():
    with pytest.raises(SystemExit):
        parse_args([])


async def test_run_requires_cert_san_ip_when_no_existing_cert(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    args = parse_args(
        [
            "--cert-file", str(tmp_path / "cert.pem"),
            "--key-file", str(tmp_path / "key.pem"),
            "--token-file", str(token_file),
        ]
    )
    with pytest.raises(SystemExit):
        await _run(args)


async def test_run_rejects_empty_token_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("   \n")
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    args = parse_args(
        [
            "--cert-file", str(cert_path),
            "--key-file", str(key_path),
            "--token-file", str(token_file),
        ]
    )
    with pytest.raises(SystemExit, match="empty"):
        await _run(args)
