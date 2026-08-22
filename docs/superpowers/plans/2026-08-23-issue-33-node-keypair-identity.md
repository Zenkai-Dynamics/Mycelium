# Issue #33 — Node Self-Generated Keypair Identity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A node generates and persists its own Ed25519 keypair on first run; the coordinator's registration handshake requires a valid signed proof of key possession alongside Phase 0's unchanged shared token, and re-keys `NodeRegistry` on the public key instead of the free-text `node_id` so a stranger can no longer hijack another node's chosen label.

**Architecture:** A new shared `mycelium/crypto.py` module owns the Ed25519 primitives (generate/sign/verify/fingerprint) used by both sides. `mycelium/node/identity.py` persists a node's keypair locally (mirrors `certs.py`'s generate-if-missing pattern). `NodeRegistry` (coordinator) re-keys from `node_id` to the base64 public key; `server.py`'s registration handler verifies the signature before registering. `node/registration.py` and `node/cli.py` carry the new fields end to end. `status_cli.py` surfaces a short display fingerprint.

**Tech Stack:** Python 3.11, `cryptography==50.0.0` (Ed25519), `websockets==17.0.1`, `pytest`/`pytest-asyncio`.

## Global Constraints

- Wire encoding for `public_key`/`signature`: base64 of raw bytes.
- Signed payload: the raw public-key bytes themselves (not a coordinator challenge, not `{node_id, model}`).
- `NodeRegistry` keys on the full base64 public-key string — never a truncated value.
- Display fingerprint: first 12 hex chars of `sha256(raw public key bytes)`, computed only at display time (never used as a registry key).
- Persisted node key: PEM/PKCS8, path from a new `--node-key-file` flag (default `~/.mycelium/node-key.pem`), auto-generated if missing, corrupt-existing-file fails loudly.
- Rejection reuses the existing `registration_rejected` message type with a new `reason` string — no new message type.
- Check order in `_handle_registration`: token → `node_id`/`model` required → `public_key`/`signature` required → signature valid.
- `node_id` stays required, unchanged validation — it is now a display label only, no security meaning.
- Phase 0's shared token remains the only admission gate — this ticket adds the identity layer alongside it, does not replace it.
- No backward-compatible dual-mode registration for not-yet-upgraded nodes.
- Full design rationale: [docs/superpowers/specs/2026-08-23-issue-33-node-keypair-identity-design.md](../specs/2026-08-23-issue-33-node-keypair-identity-design.md).

---

## Task 1: Shared Ed25519 primitives (`mycelium/crypto.py`)

**Files:**
- Modify: `pyproject.toml` (dependency move)
- Create: `src/mycelium/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Produces: `generate_keypair() -> ed25519.Ed25519PrivateKey`; `public_key_b64(private_key) -> str`; `sign_public_key(private_key) -> str`; `verify_registration_signature(public_key_b64_str, signature_b64_str) -> bool` (never raises); `fingerprint(public_key_b64_str: str) -> str` (12 hex chars).

- [ ] **Step 1: Move `cryptography` from the `coordinator` extra to base dependencies**

In `pyproject.toml`, change:

```toml
dependencies = [
    "websockets==17.0.1",
]
```

to:

```toml
dependencies = [
    "websockets==17.0.1",
    "cryptography==50.0.0",
]
```

And remove the now-redundant line from `[project.optional-dependencies].coordinator`, leaving it empty of that entry (delete the `coordinator = ["cryptography==50.0.0"]` block entirely, since `coordinator` had no other deps — confirm by reading the current file first; if `coordinator` extra becomes empty, remove the key, don't leave `coordinator = []`).

Run: `.venv/bin/pip install -e .` to confirm the base install still resolves.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_crypto.py`:

```python
"""Tests for mycelium.crypto."""

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from mycelium import crypto


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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_crypto.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.crypto'`

- [ ] **Step 4: Write the implementation**

Create `src/mycelium/crypto.py`:

```python
"""Shared Ed25519 identity primitives for node self-generated keypairs.

Used by both mycelium.node (generates, persists, signs — see
node/identity.py) and mycelium.coordinator (verifies, fingerprints for
display — see coordinator/registry.py, coordinator/server.py). Kept as a
single shared module so both sides agree on exactly what gets signed and
how it's encoded on the wire. See the design doc for issue #33.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

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
    except (binascii.Error, ValueError, InvalidSignature):
        return False
    return True


def fingerprint(public_key_b64_str: str) -> str:
    """Short SHA-256-based fingerprint for display only (status output,
    logs) — never used to decide identity. Assumes public_key_b64_str is
    already a validated, registered node's public key (callers only ever
    have one of those), so malformed input isn't handled defensively
    here the way verify_registration_signature handles it."""
    raw_public_key = base64.b64decode(public_key_b64_str)
    return hashlib.sha256(raw_public_key).hexdigest()[:FINGERPRINT_LENGTH]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_crypto.py -v`
Expected: PASS (11 tests)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/mycelium/crypto.py tests/test_crypto.py
git commit -m "feat: shared Ed25519 identity primitives (issue #33)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Node-side persisted keypair (`mycelium/node/identity.py`)

**Files:**
- Create: `src/mycelium/node/identity.py`
- Test: `tests/node/test_identity.py`

**Interfaces:**
- Consumes: `mycelium.crypto.generate_keypair() -> ed25519.Ed25519PrivateKey` (Task 1)
- Produces: `DEFAULT_KEY_PATH: Path`; `load_or_create_keypair(key_path: Path) -> ed25519.Ed25519PrivateKey`

- [ ] **Step 1: Write the failing tests**

Create `tests/node/test_identity.py`:

```python
"""Tests for mycelium.node.identity."""

import stat

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
    except (ValueError, serialization.UnsupportedAlgorithm):
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/node/test_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.node.identity'`

- [ ] **Step 3: Write the implementation**

Create `src/mycelium/node/identity.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/node/test_identity.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/identity.py tests/node/test_identity.py
git commit -m "feat: node-side persisted Ed25519 keypair (issue #33)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: `NodeRegistry` re-keys on public key, adds fingerprint

**Files:**
- Modify: `src/mycelium/coordinator/registry.py` (whole file — `Node` dataclass and every `NodeRegistry` method)
- Modify (rewrite): `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: `mycelium.crypto.fingerprint(public_key_b64_str: str) -> str` (Task 1)
- Produces: `Node(public_key: str, node_id: str, model: str, websocket: Any, pending: dict)`; `NodeRegistry.register(public_key: str, node_id: str, model: str, websocket: Any) -> Node | None`; `NodeRegistry.unregister(public_key: str, websocket: Any) -> None`; `NodeRegistry.get(public_key: str) -> Node | None`; `NodeRegistry.find_node_for_model(model: str, exclude: frozenset[str] = frozenset()) -> Node | None` (`exclude` now holds public keys); `NodeRegistry.list_nodes() -> list[dict]` with keys `node_id`, `model`, `fingerprint`. `check_token` is unchanged.

- [ ] **Step 1: Write the failing tests (full rewrite of `tests/coordinator/test_registry.py`)**

Replace the entire file with:

```python
"""Tests for mycelium.coordinator.registry."""

import pytest

from mycelium import crypto
from mycelium.coordinator.registry import NodeRegistry


def test_check_token_accepts_matching_token():
    registry = NodeRegistry("secret")
    assert registry.check_token("secret") is True


def test_check_token_rejects_wrong_token():
    registry = NodeRegistry("secret")
    assert registry.check_token("wrong") is False


def test_check_token_rejects_missing_token():
    registry = NodeRegistry("secret")
    assert registry.check_token("") is False


def test_register_adds_node_to_list():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "Qwen/Qwen2.5-7B-Instruct", websocket="ws-a")
    assert registry.list_nodes() == [
        {"node_id": "node-a", "model": "Qwen/Qwen2.5-7B-Instruct", "fingerprint": crypto.fingerprint("pubkey-a")}
    ]


def test_register_returns_none_when_no_prior_entry():
    registry = NodeRegistry("secret")
    superseded = registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    assert superseded is None


def test_register_replacing_same_public_key_returns_superseded_entry():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-old")
    superseded = registry.register("pubkey-a", "node-a", "model-b", websocket="ws-new")
    assert superseded is not None
    assert superseded.websocket == "ws-old"
    assert registry.list_nodes() == [
        {"node_id": "node-a", "model": "model-b", "fingerprint": crypto.fingerprint("pubkey-a")}
    ]


def test_different_public_keys_with_same_node_id_do_not_collide():
    """The core anti-hijack property this ticket exists for: a stranger
    registering under a node_id someone else already uses must not
    supersede them, since identity is now the public key, not the label."""
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    registry.register("pubkey-b", "node-a", "model-a", websocket="ws-b")

    nodes = registry.list_nodes()
    assert len(nodes) == 2
    assert {n["fingerprint"] for n in nodes} == {crypto.fingerprint("pubkey-a"), crypto.fingerprint("pubkey-b")}


def test_unregister_removes_matching_connection():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    registry.unregister("pubkey-a", websocket="ws-a")
    assert registry.list_nodes() == []


def test_unregister_does_not_remove_a_newer_replacement():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-old")
    registry.register("pubkey-a", "node-a", "model-b", websocket="ws-new")
    # The old connection's handler notices it's closed and tries to clean up
    # its own (now-superseded) entry — must not delete the newer one.
    registry.unregister("pubkey-a", websocket="ws-old")
    assert registry.list_nodes() == [
        {"node_id": "node-a", "model": "model-b", "fingerprint": crypto.fingerprint("pubkey-a")}
    ]


def test_registry_rejects_empty_token():
    with pytest.raises(ValueError):
        NodeRegistry("")


def test_check_token_rejects_non_string_token():
    registry = NodeRegistry("secret")
    assert registry.check_token(None) is False
    assert registry.check_token(123) is False
    assert registry.check_token(["secret"]) is False


def test_find_node_for_model_returns_matching_node():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    node = registry.find_node_for_model("model-a")
    assert node is not None
    assert node.public_key == "pubkey-a"


def test_find_node_for_model_returns_none_when_no_match():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    assert registry.find_node_for_model("model-b") is None


def test_find_node_for_model_returns_none_when_registry_empty():
    registry = NodeRegistry("secret")
    assert registry.find_node_for_model("model-a") is None


def test_find_node_for_model_round_robins_across_matching_nodes():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    registry.register("pubkey-b", "node-b", "model-a", websocket="ws-b")

    first = registry.find_node_for_model("model-a")
    second = registry.find_node_for_model("model-a")
    third = registry.find_node_for_model("model-a")

    assert [first.public_key, second.public_key, third.public_key] == ["pubkey-a", "pubkey-b", "pubkey-a"]


def test_find_node_for_model_exclude_skips_given_public_keys():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    registry.register("pubkey-b", "node-b", "model-a", websocket="ws-b")

    node = registry.find_node_for_model("model-a", exclude=frozenset({"pubkey-a"}))

    assert node.public_key == "pubkey-b"


def test_find_node_for_model_exclude_all_candidates_returns_none():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")

    assert registry.find_node_for_model("model-a", exclude=frozenset({"pubkey-a"})) is None


def test_find_node_for_model_round_robin_restarts_when_last_returned_node_is_gone():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    registry.register("pubkey-b", "node-b", "model-a", websocket="ws-b")
    registry.register("pubkey-c", "node-c", "model-a", websocket="ws-c")

    first = registry.find_node_for_model("model-a")
    assert first.public_key == "pubkey-a"

    registry.unregister("pubkey-a", websocket="ws-a")

    second = registry.find_node_for_model("model-a")
    assert second.public_key == "pubkey-b"


def test_find_node_for_model_exclude_does_not_mutate_rotation_state():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    registry.register("pubkey-b", "node-b", "model-a", websocket="ws-b")
    registry.register("pubkey-c", "node-c", "model-a", websocket="ws-c")

    # Fresh call: sets rotation state to pubkey-a
    first = registry.find_node_for_model("model-a")
    assert first.public_key == "pubkey-a"

    # Retry call with exclude: returns pubkey-b but does NOT update rotation state
    retry = registry.find_node_for_model("model-a", exclude=frozenset({"pubkey-a"}))
    assert retry.public_key == "pubkey-b"

    # Second fresh call: continues from pubkey-a (not from retry's pubkey-b),
    # so next should be pubkey-b
    second = registry.find_node_for_model("model-a")
    assert second.public_key == "pubkey-b"


def test_get_returns_registered_node():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    node = registry.get("pubkey-a")
    assert node is not None
    assert node.node_id == "node-a"
    assert node.websocket == "ws-a"


def test_get_returns_none_for_unknown_public_key():
    registry = NodeRegistry("secret")
    assert registry.get("pubkey-a") is None


def test_new_node_has_empty_pending_dict():
    registry = NodeRegistry("secret")
    registry.register("pubkey-a", "node-a", "model-a", websocket="ws-a")
    assert registry.get("pubkey-a").pending == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: FAIL — `TypeError: register() takes 4 positional arguments but 5 were given` (old signature is `register(self, node_id, model, websocket)`), plus `AttributeError`/`KeyError` on `node.public_key` and the missing `fingerprint` key.

- [ ] **Step 3: Rewrite the implementation**

Replace `src/mycelium/coordinator/registry.py` in full:

```python
"""In-memory registry of currently-registered nodes.

See the design doc for issue #8, and for the re-keying to the node's
public key, the design doc for issue #33. Tracks which nodes are
registered, the model each hosts, and the live connection to reach them.
Mutated only from the coordinator's single asyncio event loop — no
locking needed, since plain dict operations don't yield control
mid-mutation.
"""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass, field
from typing import Any

from mycelium import crypto


@dataclass
class Node:
    # The node's on-the-wire identity (base64 Ed25519 public key) — see
    # the design doc for issue #33. This, not node_id, is what the
    # registry keys on and what a reconnect is recognized by.
    public_key: str
    # A human-readable label only, as of issue #33 — no longer unique,
    # no longer a supersede/hijack vector. Two different public keys may
    # share the same node_id without colliding.
    node_id: str
    model: str
    websocket: Any
    # In-flight routed requests on this node's connection, keyed by
    # request_id — see the design doc for issue #10. Lives on the Node
    # itself (not a separate coordinator-wide dict) so it's tied to this
    # exact connection's lifetime: when the connection goes away, whoever
    # cleans it up already has this dict via the Node reference they
    # captured at registration time.
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class NodeRegistry:
    """Holds the shared token and the current set of registered nodes."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("token must not be empty")
        self._token = token
        self._nodes: dict[str, Node] = {}  # keyed by public_key
        # model -> public_key this returned last, for round-robin
        # selection in find_node_for_model. See the design doc for issue
        # #11 (mechanism) and issue #33 (keyed by public_key, not node_id).
        self._last_returned: dict[str, str] = {}

    def check_token(self, token: Any) -> bool:
        """Constant-time comparison against the configured token. Returns
        False (never raises) for a token that isn't a comparable str —
        e.g. a client sending {"token": null} or a non-ASCII value, both
        of which would otherwise raise inside hmac.compare_digest."""
        if not isinstance(token, str):
            return False
        try:
            return hmac.compare_digest(token, self._token)
        except TypeError:
            return False

    def register(self, public_key: str, node_id: str, model: str, websocket: Any) -> Node | None:
        """Add or replace public_key's entry. Returns the superseded Node
        if one existed under this exact public_key, else None — the
        caller is responsible for closing the superseded connection. Two
        different public_keys sharing the same node_id do not collide —
        see the design doc for issue #33."""
        previous = self._nodes.get(public_key)
        self._nodes[public_key] = Node(
            public_key=public_key, node_id=node_id, model=model, websocket=websocket
        )
        return previous

    def unregister(self, public_key: str, websocket: Any) -> None:
        """Remove public_key's entry, but only if it's still this exact
        connection — a newer registration may have already replaced it."""
        current = self._nodes.get(public_key)
        if current is not None and current.websocket is websocket:
            del self._nodes[public_key]

    def find_node_for_model(
        self, model: str, exclude: frozenset[str] = frozenset()
    ) -> Node | None:
        """Return the next registered node hosting `model`, round-robin
        across current candidates (skipping any public_key in `exclude`),
        or None if none match — see the design doc for issue #11.

        Rotation state is the public_key this returned last time for
        `model`; the next call returns the candidate after it, wrapping
        around. If that node has since left the registry (or is itself
        excluded), rotation restarts from the front of the current
        candidate list — no fairness guarantee across registry churn,
        only "don't always pick the same node when several are healthy."

        `exclude` lets a caller retry with a different node within one
        client request (see server.py's failover loop) without disturbing
        the cross-request rotation state.
        """
        candidates = [
            node
            for node in self._nodes.values()
            if node.model == model and node.public_key not in exclude
        ]
        if not candidates:
            return None

        start = 0
        last = self._last_returned.get(model)
        if last is not None:
            keys = [node.public_key for node in candidates]
            if last in keys:
                start = (keys.index(last) + 1) % len(candidates)

        node = candidates[start]
        if not exclude:
            self._last_returned[model] = node.public_key
        return node

    def get(self, public_key: str) -> Node | None:
        """Return the currently-registered Node for public_key, or None.

        Used by the connection-handling task right after it calls
        register(), to capture a stable reference to its own entry for
        later cleanup — see the design doc for issue #10 on why that
        reference must be captured once and never re-fetched later: a
        later re-fetch could return a *different* connection's Node if
        this one has since been superseded by a reconnect under the same
        public_key.
        """
        return self._nodes.get(public_key)

    def list_nodes(self) -> list[dict]:
        return [
            {
                "node_id": n.node_id,
                "model": n.model,
                "fingerprint": crypto.fingerprint(n.public_key),
            }
            for n in self._nodes.values()
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: PASS (21 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: re-key NodeRegistry on public key instead of node_id (issue #33)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Coordinator registration verifies the signature; routing keys on public key

**Files:**
- Modify: `src/mycelium/coordinator/server.py` (`_handle_registration`, `_handle_complete_request`)
- Modify (rewrite): `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `mycelium.crypto.generate_keypair`, `public_key_b64`, `sign_public_key`, `verify_registration_signature`, `fingerprint` (Task 1); `NodeRegistry.register(public_key, node_id, model, websocket)`, `.unregister(public_key, websocket)`, `.get(public_key)`, `.find_node_for_model(model, exclude)` (Task 3).
- Produces: unchanged public message contract, except `register` now requires `public_key`/`signature` fields and `registration_rejected` gains two new possible `reason` strings: `"public_key and signature are required"` and `"invalid signature"`.

- [ ] **Step 1: Write the failing tests (rewrite of `tests/coordinator/test_server.py`)**

This file has ~30 registration call sites. Add this helper near the top (after the existing imports and `_client_ssl_context`), then apply the transformations below.

Add the import and helper:

```python
from mycelium import crypto
```

```python
def _register_payload(node_id: str, model: str, token: str = "secret-token") -> tuple[dict, str]:
    """Build a valid register message with a freshly generated Ed25519
    keypair. Returns (payload, public_key_b64) so callers can compute the
    expected fingerprint for status-query assertions. Reconnect tests that
    need the SAME key across two registrations build their payload dicts
    directly instead of through this helper (see the semantic-rework
    tests below) — it exists to keep the many one-shot, fresh-key call
    sites for other tests short."""
    private_key = crypto.generate_keypair()
    public_key_b64 = crypto.public_key_b64(private_key)
    signature_b64 = crypto.sign_public_key(private_key)
    payload = {
        "type": "register",
        "token": token,
        "model": model,
        "node_id": node_id,
        "public_key": public_key_b64,
        "signature": signature_b64,
    }
    return payload, public_key_b64
```

Now apply these transformations. **Unaffected — do not change** (these test rejection paths reached before the new public_key/signature checks, so they need no new fields): `test_node_can_connect_over_tls`, `test_multiple_nodes_can_connect_simultaneously`, `test_connection_with_wrong_pinned_cert_is_rejected`, `test_server_survives_abnormal_disconnect`, `test_registration_with_invalid_token_is_rejected_and_closed`, `test_registration_with_missing_token_is_rejected` (the one sending `{"type": "register", "model": "m", "node_id": "node-a"}` with no `token` key), `test_connection_with_no_message_is_closed_after_timeout`, `test_registration_with_non_dict_json_is_closed_not_crashed`, `test_registration_with_null_token_is_rejected_not_crashed`, `test_complete_request_with_no_matching_node_returns_error`, `test_complete_request_with_no_healthy_node_fails_fast_with_no_retry`, `test_complete_request_with_wrong_token_is_closed_without_reply`, `test_complete_request_with_missing_prompt_returns_error`.

**Mechanical rewrite** — for each of the following tests, replace the literal `await ws.send(json.dumps({"type": "register", "token": "secret-token", "model": M, "node_id": N}))` (or `node_ws`/`old_ws`/`new_ws` variants) with:

```python
payload, public_key = _register_payload(N, M)
await ws.send(json.dumps(payload))
```

and where the test asserts on `response["nodes"]` / the final `nodes ==` value containing `{"node_id": N, "model": M}`, add the fingerprint: `{"node_id": N, "model": M, "fingerprint": crypto.fingerprint(public_key)}`. Apply this to: `test_valid_registration_is_accepted`, `test_registered_node_appears_in_status_query`, `test_disconnected_node_is_removed_from_registry`, `test_silently_unresponsive_node_is_dropped_within_ping_timeout_window`, `test_complete_request_routes_to_registered_node_and_returns_result`, `test_complete_request_node_reports_failure_is_relayed_to_client`, `test_complete_request_node_disconnect_mid_request_fails_fast`, `test_complete_request_client_disconnect_before_reply_does_not_crash_server`, `test_concurrent_complete_requests_to_same_node_get_correct_replies`.

For `test_complete_request_round_robins_across_two_healthy_nodes`, use two separate `_register_payload` calls (one per node — `node-a`/`node-b`), each generating its own key; no fingerprint assertions in this test, no other change needed.

**Semantic rework — same public key across both registrations** (these test *reconnect* behavior, which after issue #33 is keyed on public key, not node_id). Rename and rewrite:

`test_duplicate_node_id_replaces_and_closes_old_connection` → `test_duplicate_public_key_replaces_and_closes_old_connection`:

```python
async def test_duplicate_public_key_replaces_and_closes_old_connection(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        old_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        private_key = crypto.generate_keypair()
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)
        await old_ws.send(json.dumps({
            "type": "register", "token": "secret-token", "model": "model-a",
            "node_id": "node-a", "public_key": public_key, "signature": signature,
        }))
        await old_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_ws:
            await new_ws.send(json.dumps({
                "type": "register", "token": "secret-token", "model": "model-b",
                "node_id": "node-a", "public_key": public_key, "signature": signature,
            }))
            await new_ws.recv()

            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await old_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"] == [
                    {"node_id": "node-a", "model": "model-b", "fingerprint": crypto.fingerprint(public_key)}
                ]
```

Apply the identical "generate one keypair, reuse its public_key/signature across both registration sends" pattern to `test_duplicate_node_id_registration_acks_promptly_even_if_old_connection_is_unresponsive` and `test_superseded_node_connection_fails_only_its_own_pending_requests` (keep their existing names and remaining logic — only the two `json.dumps({"type": "register", ...})` payloads change, to share one generated key instead of using none).

**New test** — add after `test_duplicate_public_key_replaces_and_closes_old_connection`, this is the money test for the whole re-keying decision:

```python
async def test_different_public_keys_with_same_node_id_both_remain_registered(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws_a:
            payload_a, public_key_a = _register_payload("node-a", "model-a")
            await ws_a.send(json.dumps(payload_a))
            await ws_a.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws_b:
                # A stranger claiming the SAME node_id with a DIFFERENT
                # keypair must not supersede the first node — this is the
                # hijack issue #33 exists to close.
                payload_b, public_key_b = _register_payload("node-a", "model-a")
                await ws_b.send(json.dumps(payload_b))
                await ws_b.recv()

                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                    await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                    response = json.loads(await status_ws.recv())
                    fingerprints = {n["fingerprint"] for n in response["nodes"]}
                    assert len(response["nodes"]) == 2
                    assert fingerprints == {crypto.fingerprint(public_key_a), crypto.fingerprint(public_key_b)}
```

**New tests** — add after the existing token/missing-field rejection tests (near `test_registration_with_null_token_is_rejected_not_crashed`):

```python
async def test_registration_missing_public_key_or_signature_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "public_key and signature are required",
            }


async def test_registration_with_invalid_signature_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            key_a = crypto.generate_keypair()
            key_b = crypto.generate_keypair()
            await ws.send(json.dumps({
                "type": "register", "token": "secret-token", "model": "m", "node_id": "node-a",
                "public_key": crypto.public_key_b64(key_a),
                "signature": crypto.sign_public_key(key_b),  # signed by the WRONG key
            }))
            response = json.loads(await ws.recv())
            assert response == {"type": "registration_rejected", "reason": "invalid signature"}
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()
```

**Direct-`NodeRegistry` call sites** (bottom of the file — these bypass `_handle_registration` entirely and call `registry.register(...)` directly, so they don't need real cryptographic keys, just distinct *valid-base64* strings — **not** arbitrary placeholder text like `"pubkey-a"`: `NodeRegistry.list_nodes()` calls `crypto.fingerprint()` on every stored `public_key`, and `crypto.fingerprint()` is deliberately non-defensive about malformed input (Task 1's reviewed contract — do not weaken it to accommodate a test fixture; see Task 3's fix-round history in the ledger if this class of mistake recurs). Define near the top of the file: `import base64` (if not already imported) and module-level constants `PUBKEY_A = base64.b64encode(b"a" * 32).decode()`, `PUBKEY_B = base64.b64encode(b"b" * 32).decode()`. Rewrite `test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead`, `test_complete_request_does_not_fail_over_on_timeout`, and `test_complete_request_returns_error_when_every_node_is_dead`'s `registry.register(...)` calls from `registry.register("node-a", "m", dead_ws)` to `registry.register(PUBKEY_A, "node-a", "m", dead_ws)` (and `"node-b"` → `registry.register(PUBKEY_B, "node-b", "m", healthy_ws)`, etc. — reorder every positional call to `(public_key, node_id, model, websocket)`). Update their `registry.list_nodes()` assertions to include `fingerprint`, computed independently of `crypto.fingerprint()` itself (avoid a tautological assertion): `import hashlib` and `hashlib.sha256(b"b" * 32).hexdigest()[:12]`, e.g. `[{"node_id": "node-b", "model": "m", "fingerprint": hashlib.sha256(b"b" * 32).hexdigest()[:12]}]`, and `registry.get("node-b")` → `registry.get(PUBKEY_B)` inside `test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead`'s `reply_from_node_b` helper.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_server.py -v`
Expected: FAIL — registration attempts get rejected with `"public_key and signature are required"` instead of succeeding (since `server.py` doesn't check/use these fields yet), and the direct `registry.register(...)` calls raise `TypeError` for the old 3-arg signature.

- [ ] **Step 3: Implement the changes in `server.py`**

In `src/mycelium/coordinator/server.py`, add the import:

```python
from mycelium import crypto
```

Replace `_handle_registration` in full:

```python
async def _handle_registration(websocket, registry: NodeRegistry, message: dict) -> None:
    if not registry.check_token(message.get("token")):
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "invalid or missing token"}
        ))
        await websocket.close()
        return

    node_id = message.get("node_id")
    model = message.get("model")
    if not node_id or not model:
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "node_id and model are required"}
        ))
        await websocket.close()
        return

    public_key = message.get("public_key")
    signature = message.get("signature")
    if not public_key or not signature:
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "public_key and signature are required"}
        ))
        await websocket.close()
        return

    if not crypto.verify_registration_signature(public_key, signature):
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "invalid signature"}
        ))
        await websocket.close()
        return

    superseded = registry.register(public_key, node_id, model, websocket)
    # Captured once, right now — never re-fetched from the registry later.
    # If this node reconnects again before this connection's cleanup runs,
    # a fresh registry.get(public_key) at that point would return the
    # *newer* connection's Node, not this one. See the design doc for
    # issue #10.
    node = registry.get(public_key)
    # Ack the new connection FIRST — closing a superseded connection can
    # block for its full close_timeout if that connection is a half-dead
    # zombie (the common case: a node reconnecting after a network blip,
    # before the old socket's own ping/pong has noticed it's gone). The
    # new node's registration must not wait behind that cleanup.
    await websocket.send(json.dumps({"type": "registered"}))
    if superseded is not None:
        _close_in_background(superseded.websocket)

    try:
        async for raw in websocket:
            _dispatch_node_message(node, raw)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        registry.unregister(public_key, websocket)
        # Anything still waiting on this connection (issue #10) needs to
        # fail now, not sit out the full route_request timeout for a node
        # that's already visibly gone — whether cleanly closed, silently
        # timed out via ping/pong (#9), or superseded by a reconnect
        # (_close_in_background above, which triggers this same cleanup
        # for the *old* connection's own _handle_registration task).
        for pending_future in node.pending.values():
            if not pending_future.done():
                pending_future.set_exception(
                    router.NodeDisconnectedError(f"node {node_id!r} disconnected mid-request")
                )
```

In `_handle_complete_request`, change the failover loop's variable naming and calls from `node_id`-keyed to `public_key`-keyed:

```python
    tried: set[str] = set()
    while True:
        try:
            node = registry.find_node_for_model(model, exclude=frozenset(tried))
            if node is None:
                raise router.NoHealthyNodeError(f"no healthy node for model {model!r}")
            text = await router.route_request(
                node, prompt, timeout=router.NODE_COMPLETE_TIMEOUT_SECONDS
            )
        except router.NodeDisconnectedError:
            registry.unregister(node.public_key, node.websocket)
            tried.add(node.public_key)
            continue
```

(only the two `node.node_id` references in this loop's `except router.NodeDisconnectedError:` block change to `node.public_key` — everything else in `_handle_complete_request` is unchanged, including its comments).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_server.py -v`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: coordinator verifies node signature, routes by public key (issue #33)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: Node registration message carries `public_key`/`signature`

**Note added during Task 4's fix-loop:** `tests/test_integration.py` has two
direct calls to `registration.register(websocket, token=..., model=...,
node_id=...)` (old positional style) that this task's signature change
will break with a `TypeError` (missing required `public_key`/`signature`)
unless also fixed here — this file was missed when the plan was
originally written (Task 4 found and fixed two *other* similarly-missed
files — `tests/coordinator/test_router.py` and `tests/client/test_cli.py`
— that were fixable without this task's registration.py change; this one
genuinely couldn't be fixed until this task lands, since it calls
`registration.register()` directly, whose signature only gains
`public_key`/`signature` in this task).

**Files:**
- Modify: `src/mycelium/node/registration.py` (`register` function)
- Modify: `tests/node/test_registration.py` (every call site)
- Modify: `tests/test_integration.py` (two `registration.register(...)` call sites)

**Interfaces:**
- Produces: `register(websocket, token: str, model: str, node_id: str, public_key: str, signature: str, timeout: float = REGISTRATION_TIMEOUT_SECONDS) -> None` — raises `RegistrationRejected`/`RegistrationTimeout` exactly as before.

- [ ] **Step 1: Update the failing tests**

In `tests/node/test_registration.py`, add every call site's two new keyword arguments. `registration.py`'s own tests don't validate the keys cryptographically (that's the coordinator's job, tested in Task 4) — plain placeholder strings are sufficient here. Update every `registration.register(ws, token=..., model=..., node_id=...)` call across all 8 test functions in this file to:

```python
await registration.register(
    ws, token="secret", model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a",
    public_key="pubkey-a", signature="sig-a",
)
```

(matching each test's existing `token`/`model`/`node_id` values — only add `public_key="pubkey-a", signature="sig-a"` to each call).

And in `test_register_succeeds_and_returns_on_registered_response`, update the final assertion:

```python
    assert received == {
        "type": "register",
        "token": "secret",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "node_id": "node-a",
        "public_key": "pubkey-a",
        "signature": "sig-a",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/node/test_registration.py -v`
Expected: FAIL with `TypeError: register() got an unexpected keyword argument 'public_key'`

- [ ] **Step 3: Implement the change**

In `src/mycelium/node/registration.py`, replace the `register` function:

```python
async def register(
    websocket,
    token: str,
    model: str,
    node_id: str,
    public_key: str,
    signature: str,
    timeout: float = REGISTRATION_TIMEOUT_SECONDS,
) -> None:
    """Send the registration message and wait for the coordinator's
    response. Returns normally on success. Raises RegistrationRejected if
    the coordinator rejects the token or signature (or closes the
    connection before responding), or RegistrationTimeout if no response
    arrives in time."""
    await websocket.send(
        json.dumps({
            "type": "register",
            "token": token,
            "model": model,
            "node_id": node_id,
            "public_key": public_key,
            "signature": signature,
        })
    )
    try:
        # asyncio.timeout(), not asyncio.wait_for(): wait_for has a known
        # race on this Python version where a Task.cancel() landing at the
        # same instant the wrapped awaitable completes can be silently
        # swallowed, leaking the cancellation and hanging the caller's next
        # await forever. asyncio.timeout() doesn't have that failure mode.
        async with asyncio.timeout(timeout):
            raw = await websocket.recv()
    except TimeoutError:
        raise RegistrationTimeout(f"coordinator did not respond within {timeout}s") from None
    except websockets.exceptions.ConnectionClosed as exc:
        raise RegistrationRejected(
            f"coordinator closed the connection during registration: {exc}"
        ) from exc

    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        raise RegistrationRejected("coordinator sent a malformed response")

    if not isinstance(message, dict):
        raise RegistrationRejected(
            f"coordinator sent a non-dict response: {message!r}"
        )

    if message.get("type") == "registered":
        return
    if message.get("type") == "registration_rejected":
        raise RegistrationRejected(message.get("reason", "unknown reason"))
    raise RegistrationRejected(f"unexpected response from coordinator: {message!r}")
```

(only the signature and the sent-message dict change — everything below the `websocket.send(...)` call is untouched).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/node/test_registration.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Fix `tests/test_integration.py`'s two direct `registration.register()` calls**

Add `from mycelium import crypto` to this file's imports. In
`test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop`,
change:

```python
            await registration.register(websocket, token="secret-token", model="m", node_id="node-a")
```

to (generate once outside `node_loop`, near its other setup, so the same
identity is reused across this test's two reconnects — matching a real
node's persisted-keypair behavior):

```python
    private_key = crypto.generate_keypair()
    public_key = crypto.public_key_b64(private_key)
    signature = crypto.sign_public_key(private_key)
```

(add these three lines before `async def node_loop(port):`'s definition, then inside `node_loop`, change the call to:)

```python
            await registration.register(
                websocket, token="secret-token", model="m", node_id="node-a",
                public_key=public_key, signature=signature,
            )
```

In `test_full_round_trip_client_through_coordinator_to_node_and_back`, apply the same pattern: generate the keypair once before `async def node_loop():`'s definition, then change:

```python
                    await registration.register(
                        websocket, token="secret-token", model="m", node_id="node-a"
                    )
```

to:

```python
                    await registration.register(
                        websocket, token="secret-token", model="m", node_id="node-a",
                        public_key=public_key, signature=signature,
                    )
```

Run: `.venv/bin/pytest tests/test_integration.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, zero failures (this closes out every failure Task 4 left open).

- [ ] **Step 7: Commit**

```bash
git add src/mycelium/node/registration.py tests/node/test_registration.py tests/test_integration.py
git commit -m "feat: node registration message carries public_key/signature (issue #33)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Node CLI generates/loads its keypair and registers with it

**Files:**
- Modify: `src/mycelium/node/cli.py` (`parse_args`, `_run`)
- Modify: `tests/node/test_cli.py`

**Interfaces:**
- Consumes: `mycelium.node.identity.load_or_create_keypair(key_path) -> ed25519.Ed25519PrivateKey`, `DEFAULT_KEY_PATH` (Task 2); `mycelium.crypto.public_key_b64`, `sign_public_key` (Task 1); `mycelium.node.registration.register(..., public_key, signature)` (Task 5).
- Produces: new `--node-key-file` CLI flag, default `identity.DEFAULT_KEY_PATH`.

- [ ] **Step 1: Write the failing tests**

In `tests/node/test_cli.py`, add these two new tests (near `test_parse_args_overrides`):

```python
def test_parse_args_node_key_file_default():
    from mycelium.node import identity
    args = parse_args(["--prompt", "hi"])
    assert args.node_key_file == identity.DEFAULT_KEY_PATH


def test_parse_args_node_key_file_override():
    args = parse_args(["--prompt", "hi", "--node-key-file", "/tmp/custom-key.pem"])
    assert str(args.node_key_file) == "/tmp/custom-key.pem"
```

Update `test_run_registers_with_coordinator_using_token_and_node_id`'s final assertion — the exact-equality check now needs `public_key`/`signature` fields whose *values* aren't known ahead of time (they're freshly generated per test run), so switch from exact-dict equality to field-by-field checks plus a real signature-validity check:

```python
    from mycelium import crypto

    assert received["type"] == "register"
    assert received["token"] == "secret-token"
    assert received["model"] == vllm_process.DEFAULT_MODEL
    assert received["node_id"] == "test-node"
    assert crypto.verify_registration_signature(received["public_key"], received["signature"]) is True
```

Also add `"--node-key-file", str(tmp_path / "node-key.pem"),` to that test's `parse_args([...])` call (so it doesn't write to the real `~/.mycelium/node-key.pem` during the test run) — apply the same `--node-key-file` addition to every other test in this file that builds a coordinator-connecting `args` via `parse_args([...])` with `--coordinator-url`: `test_run_answers_a_routed_complete_request`, `test_run_retries_after_registration_rejected`, `test_registration_backoff_resets_after_a_successful_registration`. (`test_run_rejects_empty_token_file_before_starting_vllm` and `test_sigterm_stops_vllm_process_group_with_no_orphans` never reach key generation — the former exits on the empty-token check first, the latter's coordinator URL is unreachable — so they need no change.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/node/test_cli.py -v`
Expected: FAIL — `test_parse_args_node_key_file_default`/`_override` with `AttributeError: 'Namespace' object has no attribute 'node_key_file'`; the coordinator-registration tests fail with `TypeError: register() missing 2 required positional arguments: 'public_key' and 'signature'`.

- [ ] **Step 3: Implement the changes in `cli.py`**

In `src/mycelium/node/cli.py`, add imports:

```python
from mycelium import crypto
from mycelium.node import connection, identity, registration, request_handler
```

(replaces the existing `from mycelium.node import connection, registration, request_handler` line — adds `identity`).

In `parse_args`, add the new argument (after `--node-id`):

```python
    parser.add_argument("--node-key-file", type=Path, default=identity.DEFAULT_KEY_PATH)
```

In `_run`, after the existing `node_id = args.node_id or socket.gethostname()` line inside the `if args.prompt is None:` block, add:

```python
        private_key = identity.load_or_create_keypair(args.node_key_file)
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)
```

Update the `registration.register(...)` call site:

```python
                await registration.register(
                    websocket, token=token, model=args.model, node_id=node_id,
                    public_key=public_key, signature=signature,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/node/test_cli.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/cli.py tests/node/test_cli.py
git commit -m "feat: mycelium-node generates/loads its keypair and registers with it (issue #33)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: `mycelium-coordinator-status` shows each node's fingerprint

**Files:**
- Modify: `src/mycelium/coordinator/status_cli.py` (`main`)
- Modify: `tests/coordinator/test_status_cli.py`

**Interfaces:**
- Consumes: `NodeRegistry.list_nodes()`'s new `fingerprint` key (Task 3); `mycelium.node.registration.register(..., public_key, signature)` (Task 5).

- [ ] **Step 1: Update the failing test**

In `tests/coordinator/test_status_cli.py`, update `test_query_status_returns_registered_node`:

```python
async def test_query_status_returns_registered_node(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            private_key = crypto.generate_keypair()
            public_key = crypto.public_key_b64(private_key)
            await registration.register(
                node_ws, token="secret-token", model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a",
                public_key=public_key, signature=crypto.sign_public_key(private_key),
            )

            nodes = await query_status(f"wss://127.0.0.1:{port}", cert_path, "secret-token")

    assert nodes == [
        {"node_id": "node-a", "model": "Qwen/Qwen2.5-7B-Instruct", "fingerprint": crypto.fingerprint(public_key)}
    ]
```

Add the import at the top of the file: `from mycelium import crypto`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_status_cli.py -v`
Expected: FAIL — `nodes` doesn't include a `fingerprint` key (registry already produces one as of Task 3, so this specific test should already show the *actual* failure is just the missing `main()` print-format piece being untested yet; the assertion above will PASS already since `query_status`/registry plumbing was finished in Tasks 3-4). Confirm by running; if it already passes, proceed directly to Step 3's `main()` change (which has no existing automated test — see Step 3 note).

- [ ] **Step 3: Implement the `main()` display change**

In `src/mycelium/coordinator/status_cli.py`, change the print loop in `main()`:

```python
    for node in nodes:
        print(f"{node['node_id']} [{node['fingerprint']}]: {node['model']}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_status_cli.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/status_cli.py tests/coordinator/test_status_cli.py
git commit -m "feat: mycelium-coordinator-status shows each node's key fingerprint (issue #33)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 8: Document the new keypair and the co-located multi-node key-file requirement

**Files:**
- Modify: `docs/OPERATIONS.md`

No automated test — this is documentation-only, matching the design doc's decision to touch `status_cli.py`/docs directly in this ticket rather than defer to #39.

- [ ] **Step 1: Update Step 3's "What happens" list**

In `docs/OPERATIONS.md`, in the numbered list under "## Step 3 — Start a node", change item 3:

```
3. Generates (on first run only — persisted afterward) an Ed25519
   keypair at `~/.mycelium/node-key.pem` by default, override with
   `--node-key-file`. Sends a registration message (token + model +
   node ID + public key + a signature proving it holds the matching
   private key) and waits for the coordinator to ack it.
```

(replacing the existing item 3, which currently reads `Sends a registration message (token + model + node ID) and waits for the coordinator to ack it.`)

- [ ] **Step 2: Update the multi-node-per-machine caveat**

In the "**Running more than one node on the same physical machine**" paragraph (in Step 3), add a sentence after the existing `--vllm-port` caveat:

```
Each co-located node also needs its own `--node-key-file` — the default
`~/.mycelium/node-key.pem` path is shared, so two node processes on one
machine would otherwise silently load the *same* keypair and register as
one indistinguishable identity to the coordinator.
```

- [ ] **Step 3: Update Step 4's example output**

In "## Step 4 — Check what's registered", change the example output block:

```
your-hostname [a1b2c3d4e5f6]: Qwen/Qwen2.5-7B-Instruct
```

(replacing `your-hostname: Qwen/Qwen2.5-7B-Instruct`).

- [ ] **Step 4: Commit**

```bash
git add docs/OPERATIONS.md
git commit -m "docs: document node keypair and per-node --node-key-file requirement (issue #33)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 9: Full test suite and end-to-end sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: PASS, zero failures, zero errors.

- [ ] **Step 2: Manual end-to-end smoke check**

```bash
mkdir -p /tmp/mycelium-issue33-check
cd /tmp/mycelium-issue33-check
openssl rand -hex 32 > token
.venv/bin/mycelium-coordinator --token-file token --cert-san-ip 127.0.0.1 --host 127.0.0.1 --port 18765 --cert-file cert.pem --key-file key.pem &
sleep 1
# In a second terminal / after backgrounding: confirm a hand-built
# registration with a real generated keypair is accepted, and that
# mycelium-coordinator-status prints a fingerprint.
```

Since there's no real GPU/vLLM available for a true `mycelium-node` run in this environment, verify via a short Python script using `mycelium.crypto`/`websockets` directly against the running coordinator (generate a keypair, send a real `register` message, confirm `{"type": "registered"}`, then run `mycelium-coordinator-status` and confirm its output shows `<node_id> [<12-hex-fingerprint>]: <model>`). Kill the coordinator process afterward.

- [ ] **Step 3: Confirm the design doc's status line**

Update `docs/superpowers/specs/2026-08-23-issue-33-node-keypair-identity-design.md`'s `Status:` line from `Approved, not yet implemented` to `Implemented`, and commit:

```bash
git add docs/superpowers/specs/2026-08-23-issue-33-node-keypair-identity-design.md
git commit -m "docs: mark issue #33 design as implemented

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```
