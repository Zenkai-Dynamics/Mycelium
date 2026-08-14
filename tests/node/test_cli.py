"""Tests for mycelium.node.cli."""

import pytest

from mycelium.node.cli import parse_args


def test_parse_args_requires_coordinator_url():
    with pytest.raises(SystemExit):
        parse_args(["--coordinator-cert", "/tmp/cert.pem"])


def test_parse_args_requires_coordinator_cert():
    with pytest.raises(SystemExit):
        parse_args(["--coordinator-url", "wss://example:8765"])


def test_parse_args_valid():
    args = parse_args(
        ["--coordinator-url", "wss://example:8765", "--coordinator-cert", "/tmp/cert.pem"]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == "/tmp/cert.pem"
