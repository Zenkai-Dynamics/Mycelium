"""Shared Ed25519 identity primitives for node self-generated keypairs.

Used by both mycelium.node (generates, persists, signs — see
node/identity.py) and mycelium.coordinator (verifies, fingerprints for
display — see coordinator/registry.py, coordinator/server.py). Kept as a
single shared module so both sides agree on exactly what gets signed and
how it's encoded on the wire. See the design doc for issue #33.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Display-only short hash length (git-abbrev-style) — see the design doc
# for issue #33. Never used as the registry's actual key, only shown to
# a human (mycelium-coordinator-status, logs).
FINGERPRINT_LENGTH = 12


def generate_keypair() -> ed25519.Ed25519PrivateKey:
    """Generate a new Ed25519 private key. The matching public key is
    always derivable from it via `.public_key()` — nothing else needs to
    be persisted separately."""
    return ed25519.Ed25519PrivateKey.generate()


def _raw_public_key_bytes(private_key: ed25519.Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def public_key_b64(private_key: ed25519.Ed25519PrivateKey) -> str:
    """The raw 32-byte public key, base64-encoded for the wire."""
    return base64.b64encode(_raw_public_key_bytes(private_key)).decode("ascii")


def sign_public_key(private_key: ed25519.Ed25519PrivateKey) -> str:
    """Sign the private key's own raw public-key bytes — a minimal proof
    of possession, base64-encoded for the wire. See the design doc for
    issue #33 on why the public key itself (not a coordinator-issued
    challenge) is what gets signed: registration is a single message,
    with no round trip for a challenge to travel on."""
    raw_public_key = _raw_public_key_bytes(private_key)
    signature = private_key.sign(raw_public_key)
    return base64.b64encode(signature).decode("ascii")


def verify_registration_signature(public_key_b64_str, signature_b64_str) -> bool:
    """True if signature_b64_str is a valid signature, by the key
    public_key_b64_str itself claims, over public_key_b64_str's own raw
    bytes. Never raises for any malformed input — mirrors
    NodeRegistry.check_token's "never raises, just False" contract."""
    if not isinstance(public_key_b64_str, str) or not isinstance(signature_b64_str, str):
        return False
    try:
        raw_public_key = base64.b64decode(public_key_b64_str, validate=True)
        raw_signature = base64.b64decode(signature_b64_str, validate=True)
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(raw_public_key)
        public_key.verify(raw_signature, raw_public_key)
    except (ValueError, InvalidSignature):
        return False
    return True


def canonical_public_key(public_key_b64_str: str) -> str:
    """Re-encode a base64 public key string to its canonical form —
    collapses the small number of non-canonical base64 encodings that
    decode to the same 32 raw bytes (unused trailing bits in the final
    character aren't checked by base64 decoding) to one string, so
    NodeRegistry's string-keyed dict can't be handed the same key twice
    under different spellings. Only ever called after
    verify_registration_signature has already confirmed public_key_b64_str
    decodes to a valid 32-byte value — this function assumes that."""
    raw_public_key = base64.b64decode(public_key_b64_str)
    return base64.b64encode(raw_public_key).decode("ascii")


def fingerprint(public_key_b64_str: str) -> str:
    """Short SHA-256-based fingerprint for display only (status output,
    logs) — never used to decide identity. Assumes public_key_b64_str is
    already a validated, registered node's public key (callers only ever
    have one of those), so malformed input isn't handled defensively
    here the way verify_registration_signature handles it."""
    raw_public_key = base64.b64decode(public_key_b64_str)
    return hashlib.sha256(raw_public_key).hexdigest()[:FINGERPRINT_LENGTH]


# Opaque-handle length, deliberately NOT equal to FINGERPRINT_LENGTH — a
# handle and a fingerprint have opposite disclosure properties (a
# fingerprint identifies a node to the operator; a handle exists so a
# client cannot identify anything), and different lengths make confusing
# one for the other visible. See the design doc for issue #63.
HANDLE_LENGTH = 16


def handle(secret: bytes, value: str) -> str:
    """Opaque, unresolvable identifier for `value`, derived under
    `secret` — used to tell a client which node and which identity served
    each hop of its flow without disclosing either.

    HMAC rather than a plain hash, specifically. A node handle's input is
    the node's base64 public key, and a client holding the shared token
    can call `status_query` (coordinator/server.py) and get back every
    registered node's `fingerprint` — which is `sha256(raw_public_key)`
    truncated to FINGERPRINT_LENGTH. So an unsalted
    `sha256(public_key)[:HANDLE_LENGTH]` handle would share its first 12
    characters with a fingerprint the client already has in hand,
    resolving a handle to a node by prefix match and no work at all. The
    per-process secret is what makes a handle groupable but not
    identifying. See the design doc for issue #63 and ADR-0004.
    """
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()[:HANDLE_LENGTH]
