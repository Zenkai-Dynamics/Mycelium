"""Node-side persistent Ed25519 keypair — generated once, reused across
restarts and reconnects. The public key is the node's on-the-wire
identity (see mycelium.crypto and the coordinator's NodeRegistry). See
the design doc for issue #33.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from mycelium import crypto

DEFAULT_KEY_PATH = Path.home() / ".mycelium" / "node-key.pem"


def load_or_create_keypair(key_path: Path) -> ed25519.Ed25519PrivateKey:
    """Load the persisted keypair at key_path, generating and persisting
    a new one first if it doesn't exist yet — same identity on every
    subsequent call. Mirrors mycelium.coordinator.certs.ensure_cert's
    generate-if-missing pattern. A corrupt/unreadable existing file fails
    loudly (whatever exception serialization.load_pem_private_key raises)
    rather than silently regenerating — losing a node's identity should
    never happen quietly."""
    if key_path.exists():
        return serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    private_key = crypto.generate_keypair()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return private_key
