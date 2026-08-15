"""Tests for mycelium.coordinator.registry."""

import pytest

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
    registry.register("node-a", "Qwen/Qwen2.5-7B-Instruct", websocket="ws-a")
    assert registry.list_nodes() == [{"node_id": "node-a", "model": "Qwen/Qwen2.5-7B-Instruct"}]


def test_register_returns_none_when_no_prior_entry():
    registry = NodeRegistry("secret")
    superseded = registry.register("node-a", "model-a", websocket="ws-a")
    assert superseded is None


def test_register_replacing_existing_node_returns_superseded_entry():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-old")
    superseded = registry.register("node-a", "model-b", websocket="ws-new")
    assert superseded is not None
    assert superseded.websocket == "ws-old"
    assert registry.list_nodes() == [{"node_id": "node-a", "model": "model-b"}]


def test_unregister_removes_matching_connection():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    registry.unregister("node-a", websocket="ws-a")
    assert registry.list_nodes() == []


def test_unregister_does_not_remove_a_newer_replacement():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-old")
    registry.register("node-a", "model-b", websocket="ws-new")
    # The old connection's handler notices it's closed and tries to clean up
    # its own (now-superseded) entry — must not delete the newer one.
    registry.unregister("node-a", websocket="ws-old")
    assert registry.list_nodes() == [{"node_id": "node-a", "model": "model-b"}]


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
    registry.register("node-a", "model-a", websocket="ws-a")
    node = registry.find_node_for_model("model-a")
    assert node is not None
    assert node.node_id == "node-a"


def test_find_node_for_model_returns_none_when_no_match():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    assert registry.find_node_for_model("model-b") is None


def test_find_node_for_model_returns_none_when_registry_empty():
    registry = NodeRegistry("secret")
    assert registry.find_node_for_model("model-a") is None


def test_find_node_for_model_round_robins_across_matching_nodes():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    registry.register("node-b", "model-a", websocket="ws-b")

    first = registry.find_node_for_model("model-a")
    second = registry.find_node_for_model("model-a")
    third = registry.find_node_for_model("model-a")

    assert [first.node_id, second.node_id, third.node_id] == ["node-a", "node-b", "node-a"]


def test_find_node_for_model_exclude_skips_given_node_ids():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    registry.register("node-b", "model-a", websocket="ws-b")

    node = registry.find_node_for_model("model-a", exclude=frozenset({"node-a"}))

    assert node.node_id == "node-b"


def test_find_node_for_model_exclude_all_candidates_returns_none():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")

    assert registry.find_node_for_model("model-a", exclude=frozenset({"node-a"})) is None


def test_find_node_for_model_round_robin_restarts_when_last_returned_node_is_gone():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    registry.register("node-b", "model-a", websocket="ws-b")
    registry.register("node-c", "model-a", websocket="ws-c")

    first = registry.find_node_for_model("model-a")
    assert first.node_id == "node-a"

    registry.unregister("node-a", websocket="ws-a")

    second = registry.find_node_for_model("model-a")
    assert second.node_id == "node-b"


def test_find_node_for_model_exclude_does_not_mutate_rotation_state():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    registry.register("node-b", "model-a", websocket="ws-b")
    registry.register("node-c", "model-a", websocket="ws-c")

    # Fresh call: sets rotation state to node-a
    first = registry.find_node_for_model("model-a")
    assert first.node_id == "node-a"

    # Retry call with exclude: returns node-b but does NOT update rotation state
    retry = registry.find_node_for_model("model-a", exclude=frozenset({"node-a"}))
    assert retry.node_id == "node-b"

    # Second fresh call: continues from node-a (not from retry's node-b),
    # so next should be node-b
    second = registry.find_node_for_model("model-a")
    assert second.node_id == "node-b"


def test_get_returns_registered_node():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    node = registry.get("node-a")
    assert node is not None
    assert node.node_id == "node-a"
    assert node.websocket == "ws-a"


def test_get_returns_none_for_unknown_node_id():
    registry = NodeRegistry("secret")
    assert registry.get("node-a") is None


def test_new_node_has_empty_pending_dict():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    assert registry.get("node-a").pending == {}
