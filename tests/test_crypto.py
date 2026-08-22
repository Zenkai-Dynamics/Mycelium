"""Tests for mycelium.crypto."""

import base64
import string

from cryptography.hazmat.primitives.asymmetric import ed25519

from mycelium import crypto

_B64_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "+/"


def _non_canonical_variant(public_key_b64_str: str) -> str:
    """Brute-force an alternate base64 spelling of the same raw bytes as
    public_key_b64_str. For a 32-byte payload, the second-to-last base64
    character carries 2 unused low bits that plain base64 decoding doesn't
    check — trying every alphabet character in that slot finds the (up to
    3) other spellings that decode to the identical bytes."""
    raw = base64.b64decode(public_key_b64_str)
    for candidate_char in _B64_ALPHABET:
        candidate = public_key_b64_str[:-2] + candidate_char + public_key_b64_str[-1]
        if candidate == public_key_b64_str:
            continue
        if base64.b64decode(candidate, validate=True) == raw:
            return candidate
    raise AssertionError(f"no non-canonical variant found for {public_key_b64_str!r}")


def test_generate_keypair_returns_ed25519_private_key():
    key = crypto.generate_keypair()
    assert isinstance(key, ed25519.Ed25519PrivateKey)


def test_generate_keypair_returns_distinct_keys_each_call():
    key_a = crypto.generate_keypair()
    key_b = crypto.generate_keypair()
    assert crypto.public_key_b64(key_a) != crypto.public_key_b64(key_b)


def test_public_key_b64_round_trips_as_32_raw_bytes():
    key = crypto.generate_keypair()
    decoded = base64.b64decode(crypto.public_key_b64(key))
    assert len(decoded) == 32


def test_sign_public_key_produces_valid_signature():
    key = crypto.generate_keypair()
    public_key_b64 = crypto.public_key_b64(key)
    signature_b64 = crypto.sign_public_key(key)
    assert crypto.verify_registration_signature(public_key_b64, signature_b64) is True


def test_verify_rejects_signature_from_a_different_key():
    key_a = crypto.generate_keypair()
    key_b = crypto.generate_keypair()
    public_key_a_b64 = crypto.public_key_b64(key_a)
    signature_from_b = crypto.sign_public_key(key_b)
    assert crypto.verify_registration_signature(public_key_a_b64, signature_from_b) is False


def test_verify_rejects_malformed_base64():
    assert crypto.verify_registration_signature("not-valid-base64!!!", "also-not-base64!!!") is False


def test_verify_rejects_wrong_length_public_key():
    key = crypto.generate_keypair()
    signature_b64 = crypto.sign_public_key(key)
    too_short = base64.b64encode(b"short").decode("ascii")
    assert crypto.verify_registration_signature(too_short, signature_b64) is False


def test_verify_rejects_non_string_inputs():
    assert crypto.verify_registration_signature(None, None) is False
    assert crypto.verify_registration_signature(123, "abc") is False


def test_fingerprint_is_12_hex_chars():
    key = crypto.generate_keypair()
    fp = crypto.fingerprint(crypto.public_key_b64(key))
    assert len(fp) == 12
    int(fp, 16)  # raises if not valid hex


def test_fingerprint_is_stable_for_the_same_key():
    key = crypto.generate_keypair()
    public_key_b64 = crypto.public_key_b64(key)
    assert crypto.fingerprint(public_key_b64) == crypto.fingerprint(public_key_b64)


def test_fingerprint_differs_for_different_keys():
    key_a = crypto.generate_keypair()
    key_b = crypto.generate_keypair()
    assert crypto.fingerprint(crypto.public_key_b64(key_a)) != crypto.fingerprint(crypto.public_key_b64(key_b))


def test_non_canonical_spelling_still_passes_signature_verification():
    """Establishes the vulnerability precondition: a non-canonical base64
    spelling of the same raw public key bytes still verifies successfully,
    since verify_registration_signature operates on decoded bytes, not the
    base64 string itself."""
    key = crypto.generate_keypair()
    canonical = crypto.public_key_b64(key)
    signature = crypto.sign_public_key(key)
    non_canonical = _non_canonical_variant(canonical)

    assert non_canonical != canonical
    assert base64.b64decode(non_canonical) == base64.b64decode(canonical)
    assert crypto.verify_registration_signature(non_canonical, signature) is True


def test_canonical_public_key_collapses_non_canonical_spelling_to_same_string():
    key = crypto.generate_keypair()
    canonical = crypto.public_key_b64(key)
    non_canonical = _non_canonical_variant(canonical)

    assert crypto.canonical_public_key(canonical) == crypto.canonical_public_key(non_canonical)


def test_canonical_public_key_of_an_already_canonical_string_is_unchanged():
    key = crypto.generate_keypair()
    canonical = crypto.public_key_b64(key)
    assert crypto.canonical_public_key(canonical) == canonical
