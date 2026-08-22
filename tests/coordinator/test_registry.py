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
