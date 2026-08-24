"""Tests for mycelium.coordinator.registry."""

import base64
import hashlib
import pytest

from mycelium.coordinator.registry import NodeRegistry

# Valid base64-encoded 32-byte public keys (matching Ed25519 length)
# Generated from raw bytes (32 bytes each) for testing purposes.
PUBKEY_A = base64.b64encode(b"a" * 32).decode()
PUBKEY_B = base64.b64encode(b"b" * 32).decode()
PUBKEY_C = base64.b64encode(b"c" * 32).decode()

FINGERPRINT_LENGTH = 12


def _expected_fingerprint(raw: bytes) -> str:
    """Independently compute expected fingerprint (SHA256 → truncate) from
    already-raw bytes; callers base64-decode first (e.g. `_expected_fingerprint(b"a" * 32)`).
    Never calls crypto.fingerprint() — this tests the formula itself."""
    return hashlib.sha256(raw).hexdigest()[:FINGERPRINT_LENGTH]


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
    registry.register(PUBKEY_A, "node-a", "Qwen/Qwen2.5-7B-Instruct", websocket="ws-a")
    assert registry.list_nodes() == [
        {
            "node_id": "node-a",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "fingerprint": _expected_fingerprint(b"a" * 32),
        }
    ]


def test_register_returns_none_when_no_prior_entry():
    registry = NodeRegistry("secret")
    superseded = registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert superseded is None


def test_register_replacing_same_public_key_returns_superseded_entry():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-old")
    superseded = registry.register(PUBKEY_A, "node-a", "model-b", websocket="ws-new")
    assert superseded is not None
    assert superseded.websocket == "ws-old"
    assert registry.list_nodes() == [
        {
            "node_id": "node-a",
            "model": "model-b",
            "fingerprint": _expected_fingerprint(b"a" * 32),
        }
    ]


def test_different_public_keys_with_same_node_id_do_not_collide():
    """The core anti-hijack property this ticket exists for: a stranger
    registering under a node_id someone else already uses must not
    supersede them, since identity is now the public key, not the label."""
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.register(PUBKEY_B, "node-a", "model-a", websocket="ws-b")

    nodes = registry.list_nodes()
    assert len(nodes) == 2
    assert {n["fingerprint"] for n in nodes} == {
        _expected_fingerprint(b"a" * 32),
        _expected_fingerprint(b"b" * 32),
    }


def test_unregister_removes_matching_connection():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.unregister(PUBKEY_A, websocket="ws-a")
    assert registry.list_nodes() == []


def test_unregister_does_not_remove_a_newer_replacement():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-old")
    registry.register(PUBKEY_A, "node-a", "model-b", websocket="ws-new")
    # The old connection's handler notices it's closed and tries to clean up
    # its own (now-superseded) entry — must not delete the newer one.
    registry.unregister(PUBKEY_A, websocket="ws-old")
    assert registry.list_nodes() == [
        {
            "node_id": "node-a",
            "model": "model-b",
            "fingerprint": _expected_fingerprint(b"a" * 32),
        }
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
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    node = registry.find_node_for_model("model-a")
    assert node is not None
    assert node.public_key == PUBKEY_A


def test_find_node_for_model_returns_none_when_no_match():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.find_node_for_model("model-b") is None


def test_find_node_for_model_returns_none_when_registry_empty():
    registry = NodeRegistry("secret")
    assert registry.find_node_for_model("model-a") is None


def test_find_node_for_model_round_robins_across_matching_nodes():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.register(PUBKEY_B, "node-b", "model-a", websocket="ws-b")

    first = registry.find_node_for_model("model-a")
    second = registry.find_node_for_model("model-a")
    third = registry.find_node_for_model("model-a")

    assert [first.public_key, second.public_key, third.public_key] == [
        PUBKEY_A,
        PUBKEY_B,
        PUBKEY_A,
    ]


def test_find_node_for_model_exclude_skips_given_public_keys():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.register(PUBKEY_B, "node-b", "model-a", websocket="ws-b")

    node = registry.find_node_for_model("model-a", exclude=frozenset({PUBKEY_A}))

    assert node.public_key == PUBKEY_B


def test_find_node_for_model_exclude_all_candidates_returns_none():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")

    assert registry.find_node_for_model("model-a", exclude=frozenset({PUBKEY_A})) is None


def test_find_node_for_model_round_robin_restarts_when_last_returned_node_is_gone():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.register(PUBKEY_B, "node-b", "model-a", websocket="ws-b")
    registry.register(PUBKEY_C, "node-c", "model-a", websocket="ws-c")

    first = registry.find_node_for_model("model-a")
    assert first.public_key == PUBKEY_A

    registry.unregister(PUBKEY_A, websocket="ws-a")

    second = registry.find_node_for_model("model-a")
    assert second.public_key == PUBKEY_B


def test_find_node_for_model_exclude_does_not_mutate_rotation_state():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.register(PUBKEY_B, "node-b", "model-a", websocket="ws-b")
    registry.register(PUBKEY_C, "node-c", "model-a", websocket="ws-c")

    # Fresh call: sets rotation state to PUBKEY_A
    first = registry.find_node_for_model("model-a")
    assert first.public_key == PUBKEY_A

    # Retry call with exclude: returns PUBKEY_B but does NOT update rotation state
    retry = registry.find_node_for_model("model-a", exclude=frozenset({PUBKEY_A}))
    assert retry.public_key == PUBKEY_B

    # Second fresh call: continues from PUBKEY_A (not from retry's PUBKEY_B),
    # so next should be PUBKEY_B
    second = registry.find_node_for_model("model-a")
    assert second.public_key == PUBKEY_B


def test_get_returns_registered_node():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    node = registry.get(PUBKEY_A)
    assert node is not None
    assert node.node_id == "node-a"
    assert node.websocket == "ws-a"


def test_get_returns_none_for_unknown_public_key():
    registry = NodeRegistry("secret")
    assert registry.get(PUBKEY_A) is None


def test_new_node_has_empty_pending_dict():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.get(PUBKEY_A).pending == {}
