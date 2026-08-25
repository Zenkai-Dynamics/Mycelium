"""Tests for mycelium.coordinator.registry."""

import base64
import hashlib
import random
import pytest

from mycelium.coordinator import github_identity
from mycelium.coordinator.registry import MissingGithubToken, NodeRegistry

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


async def _fake_verifier(github_token: str) -> github_identity.GithubIdentity:
    if github_token == "bad-token":
        raise github_identity.InvalidGithubToken("bad token")
    return github_identity.GithubIdentity(id="42", login="octocat")


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
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
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
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
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
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
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


async def test_resolve_identity_binds_and_returns_identity_with_valid_token():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")
    assert identity == github_identity.GithubIdentity(id="42", login="octocat")


async def test_resolve_identity_raises_missing_token_when_key_unbound_and_no_token():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    try:
        await registry.resolve_identity(PUBKEY_A, None)
        assert False, "expected MissingGithubToken"
    except MissingGithubToken:
        pass


async def test_resolve_identity_raises_missing_token_for_empty_string_token():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    try:
        await registry.resolve_identity(PUBKEY_A, "")
        assert False, "expected MissingGithubToken"
    except MissingGithubToken:
        pass


async def test_resolve_identity_propagates_verifier_error_for_invalid_token():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    try:
        await registry.resolve_identity(PUBKEY_A, "bad-token")
        assert False, "expected InvalidGithubToken"
    except github_identity.InvalidGithubToken:
        pass


async def test_resolve_identity_ignores_token_and_reuses_binding_on_reconnect():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    first = await registry.resolve_identity(PUBKEY_A, "good-token")
    # A DIFFERENT (even invalid) token on a later call is ignored entirely
    # once the key is bound — see the design doc for issue #39.
    second = await registry.resolve_identity(PUBKEY_A, "bad-token")
    assert second == first


async def test_resolve_identity_does_not_call_verifier_again_once_bound():
    call_count = 0

    async def counting_verifier(github_token):
        nonlocal call_count
        call_count += 1
        return github_identity.GithubIdentity(id="42", login="octocat")

    registry = NodeRegistry("secret", identity_verifier=counting_verifier)
    await registry.resolve_identity(PUBKEY_A, "good-token")
    await registry.resolve_identity(PUBKEY_A, None)
    await registry.resolve_identity(PUBKEY_A, "good-token")
    assert call_count == 1


async def test_two_different_public_keys_can_bind_independently():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    identity_a = await registry.resolve_identity(PUBKEY_A, "good-token")
    identity_b = await registry.resolve_identity(PUBKEY_B, "good-token")
    assert identity_a == identity_b  # same fake identity, different keys — no cap in this ticket (#35)


def test_list_nodes_shows_none_identity_when_never_resolved():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.list_nodes() == [
        {
            "node_id": "node-a",
            "model": "model-a",
            "fingerprint": _expected_fingerprint(b"a" * 32),
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
        }
    ]


async def test_list_nodes_shows_login_after_resolve_identity():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.list_nodes() == [
        {
            "node_id": "node-a",
            "model": "model-a",
            "fingerprint": _expected_fingerprint(b"a" * 32),
            "identity": "octocat",
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
        }
    ]


def test_node_registry_without_identity_verifier_still_constructs():
    # No identity_verifier given — must default internally, not raise or
    # require the caller to know about identity verification at all.
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.get(PUBKEY_A) is not None


def test_record_completion_increments_counter():
    registry = NodeRegistry("secret")
    registry.record_completion(PUBKEY_A)
    registry.record_completion(PUBKEY_A)
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.list_nodes()[0]["reputation"] == {
        "completions": 2, "timeouts": 0, "crashes": 0, "disconnects": 0,
    }


def test_record_timeout_increments_counter():
    registry = NodeRegistry("secret")
    registry.record_timeout(PUBKEY_A)
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.list_nodes()[0]["reputation"]["timeouts"] == 1


def test_record_crash_increments_counter():
    registry = NodeRegistry("secret")
    registry.record_crash(PUBKEY_A)
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.list_nodes()[0]["reputation"]["crashes"] == 1


def test_record_disconnect_increments_counter():
    registry = NodeRegistry("secret")
    registry.record_disconnect(PUBKEY_A)
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.list_nodes()[0]["reputation"]["disconnects"] == 1


def test_reputation_counters_survive_unregister_and_reregister():
    """The core property this ticket exists for: a disconnect's counter
    must not be discarded when the connection that triggered it is
    unregistered — see the design doc for issue #36."""
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.record_disconnect(PUBKEY_A)
    registry.unregister(PUBKEY_A, websocket="ws-a")

    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-b")  # reconnect
    assert registry.list_nodes()[0]["reputation"]["disconnects"] == 1


def test_list_nodes_shows_zero_reputation_for_node_with_no_recorded_events():
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.list_nodes()[0]["reputation"] == {
        "completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0,
    }


def test_find_node_for_model_with_equal_reputation_is_still_exact_round_robin():
    """Every candidate has recorded SOME history, but identical amounts —
    weights tie, so this must still be the deterministic sequence, not a
    weighted draw. Distinct from the zero-history case the pre-existing
    round-robin tests already cover."""
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.register(PUBKEY_B, "node-b", "model-a", websocket="ws-b")
    registry.record_completion(PUBKEY_A)
    registry.record_completion(PUBKEY_B)

    first = registry.find_node_for_model("model-a")
    second = registry.find_node_for_model("model-a")
    third = registry.find_node_for_model("model-a")
    assert [first.public_key, second.public_key, third.public_key] == [
        PUBKEY_A, PUBKEY_B, PUBKEY_A,
    ]


def test_find_node_for_model_prefers_more_reliable_node_when_weights_differ():
    registry = NodeRegistry("secret", random_source=random.Random(42))
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.register(PUBKEY_B, "node-b", "model-a", websocket="ws-b")
    for _ in range(10):
        registry.record_completion(PUBKEY_A)  # A: perfect record
    for _ in range(10):
        registry.record_crash(PUBKEY_B)  # B: all failures

    picks = [registry.find_node_for_model("model-a").public_key for _ in range(200)]
    a_share = picks.count(PUBKEY_A) / len(picks)
    assert a_share > 0.7, f"expected A to dominate selection, got {a_share:.2f} share"


def test_find_node_for_model_never_fully_excludes_unreliable_node():
    registry = NodeRegistry("secret", random_source=random.Random(7))
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    registry.register(PUBKEY_B, "node-b", "model-a", websocket="ws-b")
    for _ in range(50):
        registry.record_completion(PUBKEY_A)
    for _ in range(50):
        registry.record_crash(PUBKEY_B)

    picks = {registry.find_node_for_model("model-a").public_key for _ in range(300)}
    assert PUBKEY_B in picks, "an unreliable node must still be pickable, never fully excluded"


def test_find_node_for_model_weighted_draw_uses_injected_random_source():
    """Proves the injected random_source is actually consulted, not the
    global random module — two independently-built registries seeded
    identically must produce the identical draw sequence."""
    def build_registry():
        registry = NodeRegistry("secret", random_source=random.Random(99))
        registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
        registry.register(PUBKEY_B, "node-b", "model-a", websocket="ws-b")
        registry.record_completion(PUBKEY_A)
        registry.record_crash(PUBKEY_B)
        return registry

    registry_1 = build_registry()
    registry_2 = build_registry()
    picks_1 = [registry_1.find_node_for_model("model-a").public_key for _ in range(20)]
    picks_2 = [registry_2.find_node_for_model("model-a").public_key for _ in range(20)]
    assert picks_1 == picks_2
