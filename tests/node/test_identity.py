"""Tests for mycelium.node.identity."""

import stat

from cryptography import exceptions
from cryptography.hazmat.primitives import serialization

from mycelium import crypto
from mycelium.node.identity import load_or_create_keypair


def test_creates_key_file_when_missing(tmp_path):
    key_path = tmp_path / "node-key.pem"

    load_or_create_keypair(key_path)

    assert key_path.exists()
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


def test_key_file_has_restrictive_permissions(tmp_path):
    key_path = tmp_path / "node-key.pem"

    load_or_create_keypair(key_path)

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_does_not_regenerate_if_file_exists(tmp_path):
    key_path = tmp_path / "node-key.pem"
    first = load_or_create_keypair(key_path)
    first_public_key = crypto.public_key_b64(first)

    second = load_or_create_keypair(key_path)

    assert crypto.public_key_b64(second) == first_public_key


def test_creates_parent_directory_if_missing(tmp_path):
    key_path = tmp_path / "nested" / "dir" / "node-key.pem"

    load_or_create_keypair(key_path)

    assert key_path.exists()


def test_corrupt_existing_file_fails_loudly(tmp_path):
    key_path = tmp_path / "node-key.pem"
    key_path.write_bytes(b"not a real key")

    try:
        load_or_create_keypair(key_path)
        assert False, "expected loading a corrupt key file to raise"
    except (ValueError, exceptions.UnsupportedAlgorithm):
        pass
