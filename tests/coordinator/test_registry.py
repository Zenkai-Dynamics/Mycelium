"""Tests for mycelium.coordinator.registry."""

import base64
import hashlib
import random
import pytest

from mycelium import crypto
from mycelium.coordinator import github_identity
from mycelium.coordinator.registry import (
    IdentityBanned,
    IdentityCapReached,
    MissingGithubToken,
    NodeRegistry,
    UnknownIdentity,
)

# Valid base64-encoded 32-byte public keys (matching Ed25519 length)
# Generated from raw bytes (32 bytes each) for testing purposes.
PUBKEY_A = base64.b64encode(b"a" * 32).decode()
PUBKEY_B = base64.b64encode(b"b" * 32).decode()
PUBKEY_C = base64.b64encode(b"c" * 32).decode()
PUBKEY_D = base64.b64encode(b"d" * 32).decode()

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
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0, "client_faults": 0},
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
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0, "client_faults": 0},
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
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0, "client_faults": 0},
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
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0, "client_faults": 0},
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
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0, "client_faults": 0},
        }
    ]


def test_node_registry_without_identity_verifier_still_constructs():
    # No identity_verifier given — must default internally, not raise or
    # require the caller to know about identity verification at all.
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.get(PUBKEY_A) is not None


async def test_enforce_identity_cap_allows_registration_below_cap():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier, per_identity_cap=3)
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")

    # Only 1 of this identity's nodes is registered so far; cap is 3.
    registry.enforce_identity_cap(PUBKEY_B, identity)  # must not raise


async def test_enforce_identity_cap_raises_when_at_cap():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier, per_identity_cap=2)
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")
    await registry.resolve_identity(PUBKEY_B, "good-token")
    registry.register(PUBKEY_B, "node-b", "m", websocket="ws-b")

    try:
        registry.enforce_identity_cap(PUBKEY_C, identity)
        assert False, "expected IdentityCapReached"
    except IdentityCapReached as exc:
        assert str(exc) == "identity has reached the maximum of 2 registered nodes"


async def test_enforce_identity_cap_exempts_already_registered_key():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier, per_identity_cap=1)
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")

    # PUBKEY_A already occupies the (only) slot — re-checking the SAME key
    # (a reconnect) must not raise, even though the identity is "at" cap.
    registry.enforce_identity_cap(PUBKEY_A, identity)  # must not raise


async def test_enforce_identity_cap_frees_a_slot_on_unregister():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier, per_identity_cap=1)
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")

    try:
        registry.enforce_identity_cap(PUBKEY_B, identity)
        assert False, "expected IdentityCapReached"
    except IdentityCapReached:
        pass

    registry.unregister(PUBKEY_A, websocket="ws-a")

    registry.enforce_identity_cap(PUBKEY_B, identity)  # must not raise now


async def test_enforce_identity_cap_counts_only_the_matching_identity():
    async def two_identity_verifier(github_token):
        if github_token == "token-x":
            return github_identity.GithubIdentity(id="X", login="x-user")
        return github_identity.GithubIdentity(id="Y", login="y-user")

    registry = NodeRegistry("secret", identity_verifier=two_identity_verifier, per_identity_cap=1)
    await registry.resolve_identity(PUBKEY_A, "token-x")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")

    identity_y = await registry.resolve_identity(PUBKEY_B, "token-y")

    # Identity X is at its cap of 1, but identity Y has 0 registered nodes
    # of its own — must not be blocked by X's count.
    registry.enforce_identity_cap(PUBKEY_B, identity_y)  # must not raise


async def test_enforce_identity_cap_default_is_3():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)  # no override
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")
    await registry.resolve_identity(PUBKEY_B, "good-token")
    registry.register(PUBKEY_B, "node-b", "m", websocket="ws-b")
    await registry.resolve_identity(PUBKEY_C, "good-token")
    registry.register(PUBKEY_C, "node-c", "m", websocket="ws-c")

    try:
        registry.enforce_identity_cap(PUBKEY_D, identity)
        assert False, "expected IdentityCapReached"
    except IdentityCapReached as exc:
        assert str(exc) == "identity has reached the maximum of 3 registered nodes"


def test_enforce_identity_cap_zero_blocks_all_new_registrations():
    registry = NodeRegistry("secret", per_identity_cap=0)
    identity = github_identity.GithubIdentity(id="42", login="octocat")

    try:
        registry.enforce_identity_cap(PUBKEY_A, identity)
        assert False, "expected IdentityCapReached"
    except IdentityCapReached as exc:
        assert str(exc) == "identity has reached the maximum of 0 registered nodes"


def test_record_completion_increments_counter():
    registry = NodeRegistry("secret")
    registry.record_completion(PUBKEY_A)
    registry.record_completion(PUBKEY_A)
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.list_nodes()[0]["reputation"] == {
        "completions": 2, "timeouts": 0, "crashes": 0, "disconnects": 0, "client_faults": 0,
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


def test_record_client_fault_increments_counter():
    registry = NodeRegistry("token")
    registry.register(PUBKEY_A, "node-a", "m", object())

    registry.record_client_fault(PUBKEY_A)

    assert registry.list_nodes()[0]["reputation"]["client_faults"] == 1


def test_client_faults_do_not_affect_routing_weight():
    """The whole point: a client's mistake must never move routing. A
    node with many client faults and no real failures must weigh exactly
    the same as an untouched one."""
    registry = NodeRegistry("token")
    registry.register(PUBKEY_A, "node-a", "m", object())
    registry.register(PUBKEY_B, "node-b", "m", object())
    for _ in range(50):
        registry.record_client_fault(PUBKEY_A)

    assert registry._reputation_weight(PUBKEY_A) == registry._reputation_weight(PUBKEY_B)


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
        "completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0, "client_faults": 0,
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


async def test_enforce_not_banned_allows_unbanned_identity():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.enforce_not_banned(identity)  # must not raise


def test_ban_identity_raises_unknown_identity_for_unseen_login():
    registry = NodeRegistry("secret")
    try:
        registry.ban_identity("nobody")
        assert False, "expected UnknownIdentity"
    except UnknownIdentity:
        pass


async def test_ban_identity_then_enforce_not_banned_raises():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")

    registry.ban_identity("octocat")

    try:
        registry.enforce_not_banned(identity)
        assert False, "expected IdentityBanned"
    except IdentityBanned as exc:
        assert str(exc) == "this identity has been banned by the operator"


async def test_ban_identity_returns_currently_registered_nodes_under_that_identity():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")

    disconnected = registry.ban_identity("octocat")

    assert [node.public_key for node in disconnected] == [PUBKEY_A]


async def test_ban_identity_returns_empty_list_when_identity_has_no_registered_nodes():
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    # PUBKEY_A resolved the identity (e.g. registration is mid-flight) but
    # was never actually registered as a node.
    await registry.resolve_identity(PUBKEY_A, "good-token")

    disconnected = registry.ban_identity("octocat")

    assert disconnected == []


async def test_ban_identity_does_not_unregister_nodes():
    """ban_identity only marks the identity banned and reports which nodes
    are affected — the caller (server.py) is responsible for actually
    disconnecting them. See the design doc for issue #37."""
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")

    registry.ban_identity("octocat")

    assert registry.get(PUBKEY_A) is not None


async def test_enforce_not_banned_has_no_exemption_for_already_registered_key():
    """Unlike enforce_identity_cap, a banned identity's reconnect using its
    already-registered key must still be rejected — that's the literal
    acceptance criterion for #37."""
    registry = NodeRegistry("secret", identity_verifier=_fake_verifier)
    identity = await registry.resolve_identity(PUBKEY_A, "good-token")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")
    registry.ban_identity("octocat")

    try:
        registry.enforce_not_banned(identity)
        assert False, "expected IdentityBanned"
    except IdentityBanned:
        pass


async def test_ban_identity_only_bans_matching_identity():
    async def two_identity_verifier(github_token):
        if github_token == "token-x":
            return github_identity.GithubIdentity(id="X", login="x-user")
        return github_identity.GithubIdentity(id="Y", login="y-user")

    registry = NodeRegistry("secret", identity_verifier=two_identity_verifier)
    await registry.resolve_identity(PUBKEY_A, "token-x")
    registry.register(PUBKEY_A, "node-a", "m", websocket="ws-a")
    identity_y = await registry.resolve_identity(PUBKEY_B, "token-y")
    registry.register(PUBKEY_B, "node-b", "m", websocket="ws-b")

    registry.ban_identity("x-user")

    registry.enforce_not_banned(identity_y)  # must not raise — a different identity


async def test_handles_for_is_stable_across_calls():
    registry = NodeRegistry("token", identity_verifier=_fake_verifier, handle_secret=b"s" * 32)
    await registry.resolve_identity(PUBKEY_A, "valid-github-token")

    assert registry.handles_for(PUBKEY_A) == registry.handles_for(PUBKEY_A)


async def test_two_nodes_under_one_identity_share_an_identity_handle():
    """The property the whole feature exists for: three nodes can be one
    person, and the exposure report has to be able to say so."""
    registry = NodeRegistry("token", identity_verifier=_fake_verifier, handle_secret=b"s" * 32)
    await registry.resolve_identity(PUBKEY_A, "valid-github-token")
    await registry.resolve_identity(PUBKEY_B, "valid-github-token")

    handles_a = registry.handles_for(PUBKEY_A)
    handles_b = registry.handles_for(PUBKEY_B)

    assert handles_a["node_handle"] != handles_b["node_handle"]
    assert handles_a["identity_handle"] == handles_b["identity_handle"]


async def test_different_secrets_produce_different_handles():
    one = NodeRegistry("token", identity_verifier=_fake_verifier, handle_secret=b"a" * 32)
    two = NodeRegistry("token", identity_verifier=_fake_verifier, handle_secret=b"b" * 32)
    await one.resolve_identity(PUBKEY_A, "valid-github-token")
    await two.resolve_identity(PUBKEY_A, "valid-github-token")

    assert one.handles_for(PUBKEY_A) != two.handles_for(PUBKEY_A)


def test_handles_for_an_unbound_key_reports_a_null_identity_handle():
    """Structurally shouldn't happen — registration always binds an
    identity — but a completion must never fail because a reporting
    field couldn't be built."""
    registry = NodeRegistry("token", handle_secret=b"s" * 32)

    handles = registry.handles_for(PUBKEY_A)

    assert handles["node_handle"]
    assert handles["identity_handle"] is None


def test_handles_do_not_leak_the_key_the_fingerprint_or_the_login():
    registry = NodeRegistry("token", handle_secret=b"s" * 32)

    handles = registry.handles_for(PUBKEY_A)

    assert handles["node_handle"] != PUBKEY_A
    assert handles["node_handle"] != crypto.fingerprint(PUBKEY_A)


def test_two_registries_without_an_injected_secret_differ():
    """The default secret is per-process and random, so handles are not
    comparable across a coordinator restart — see the design doc."""
    one = NodeRegistry("token")
    two = NodeRegistry("token")

    assert one.handles_for(PUBKEY_A) != two.handles_for(PUBKEY_A)
