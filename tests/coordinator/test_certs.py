"""Tests for mycelium.coordinator.certs."""

import ipaddress
import stat

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from mycelium.coordinator.certs import ensure_cert


def test_generates_cert_and_key_files_when_missing(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_cert(cert_path, key_path, "20.244.2.48")

    assert cert_path.exists()
    assert key_path.exists()
    assert cert_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


def test_cert_has_correct_ip_san(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_cert(cert_path, key_path, "20.244.2.48")

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    ips = san.value.get_values_for_type(x509.IPAddress)
    assert ipaddress.ip_address("20.244.2.48") in ips


def test_does_not_regenerate_if_files_exist(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(b"existing-cert")
    key_path.write_bytes(b"existing-key")

    ensure_cert(cert_path, key_path, "20.244.2.48")

    assert cert_path.read_bytes() == b"existing-cert"
    assert key_path.read_bytes() == b"existing-key"


def test_key_matches_cert_public_key(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_cert(cert_path, key_path, "20.244.2.48")

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    assert cert.public_key().public_numbers() == key.public_key().public_numbers()


def test_key_file_has_restrictive_permissions(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_cert(cert_path, key_path, "20.244.2.48")

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
