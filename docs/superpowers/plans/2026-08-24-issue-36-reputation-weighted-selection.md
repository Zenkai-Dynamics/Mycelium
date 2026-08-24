# Issue #36 — Reputation-Weighted Node Selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-node completion/timeout/crash/disconnect counters, persisted across reconnects, give `NodeRegistry.find_node_for_model` a soft weighting pass — still exact round-robin when candidates are equally reliable (every case today's tests already cover), a weighted-random draw only when their track records actually differ, never a hard cutoff.

**Architecture:** A new `public_key`-keyed `_reputation` dict on `NodeRegistry` (parallel to `_identity_by_key`) survives independently of the connection-scoped `Node` object, via four small `record_*` methods. `find_node_for_model` computes a Laplace-smoothed reliability weight per candidate; when all weights tie it falls through to today's unchanged deterministic rotation, otherwise it draws via an injected `random.Random` instance. `server.py`'s `_handle_complete_request` calls the right `record_*` method at each of the three existing failure branches (now split into explicit `except` clauses) and on success, sharing response logic via a small local `reject()` helper. `status_cli.py` renders the four counts.

**Tech Stack:** Python 3.11, stdlib `random` (no new dependency). Independent of #33/#39/#35's identity work — applies equally to a fully-trusted Phase 0 pool.

## Global Constraints

- `ReputationCounters(completions: int = 0, timeouts: int = 0, crashes: int = 0, disconnects: int = 0)` — a small dataclass, one entry per `public_key` in `NodeRegistry._reputation: dict[str, ReputationCounters]`, created lazily on first event (a node with no entry is treated as `ReputationCounters()`, i.e. all-zero).
- `NodeRegistry.record_completion(public_key)` / `record_timeout(public_key)` / `record_crash(public_key)` / `record_disconnect(public_key)` — each increments exactly one field. This dict is never touched by `register()`/`unregister()` — counters persist across a node's disconnect/reconnect cycle by construction.
- `list_nodes()` gains a `"reputation"` field per node: `{"completions": N, "timeouts": N, "crashes": N, "disconnects": N}`, always present (never omitted), defaulting to all-zero for a node with no recorded events.
- Weight formula: `(completions + 1) / (completions + timeouts + crashes + disconnects + 1)` — 1.0 for a node with zero history, decreasing smoothly toward (never reaching) 0 as failures accumulate.
- `NodeRegistry.__init__` gains `random_source: random.Random | None = None`, defaulting internally to a fresh, private `random.Random()` instance — never the `random` module's shared global functions.
- `find_node_for_model`: if every current candidate's weight is identical, use today's exact deterministic rotation logic unchanged (this is the case every existing round-robin test exercises — zero reputation history everywhere). Only when weights differ does `self._random.choices(candidates, weights=weights, k=1)[0]` run instead. Either way, `self._last_returned[model]` is updated to whichever node was actually picked, exactly as today.
- `server.py`'s `_handle_complete_request`: `NodeDisconnectedError` → `record_disconnect` (before `unregister`); new explicit `except router.NodeTimeoutError` → `record_timeout`; new explicit `except router.NodeError` → `record_crash`; the existing generic `except router.RoutingError` now only ever catches `NoHealthyNodeError` (no node to record against); the success path (`else: break`) → `record_completion`. A small local `async def reject(reason): ...` closure holds the shared send-`complete_error`-and-close logic so it isn't tripled across the three now-separate except blocks.
- `mycelium-coordinator-status` renders `[ok:N timeout:N crash:N disconnect:N]` for every node, always present (no conditional omission, unlike the identity parenthetical).
- No time-based decay/aging of counters. No hard reliability cutoff. No persistence across a coordinator restart.
- Full design rationale: [docs/superpowers/specs/2026-08-24-issue-36-reputation-weighted-selection-design.md](../specs/2026-08-24-issue-36-reputation-weighted-selection-design.md).

---

## Task 1: `NodeRegistry` reputation counters

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Modify: `tests/coordinator/test_registry.py`

**Interfaces:**
- Produces: `ReputationCounters(completions: int = 0, timeouts: int = 0, crashes: int = 0, disconnects: int = 0)`; `NodeRegistry.record_completion(public_key: str) -> None`; `.record_timeout(public_key: str) -> None`; `.record_crash(public_key: str) -> None`; `.record_disconnect(public_key: str) -> None`; `NodeRegistry.list_nodes()` dicts gain a `"reputation"` key.

**Note:** this task leaves `tests/coordinator/test_server.py` and `tests/coordinator/test_status_cli.py` failing — both have their own exact-dict assertions against `list_nodes()`'s output that don't yet include the new `"reputation"` key. That's expected and fixed in Task 3 (test_server.py) and Task 4 (test_status_cli.py); this task's own file (`tests/coordinator/test_registry.py`) must fully pass on its own.

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_registry.py`, update these three existing exact-dict assertions to add `"reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0}` as the last key in each dict:

`test_register_adds_node_to_list`:
```python
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
```

`test_register_replacing_same_public_key_returns_superseded_entry` (only the final assertion changes):
```python
    assert registry.list_nodes() == [
        {
            "node_id": "node-a",
            "model": "model-b",
            "fingerprint": _expected_fingerprint(b"a" * 32),
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
        }
    ]
```

`test_unregister_does_not_remove_a_newer_replacement` (only the final assertion changes):
```python
    assert registry.list_nodes() == [
        {
            "node_id": "node-a",
            "model": "model-b",
            "fingerprint": _expected_fingerprint(b"a" * 32),
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
        }
    ]
```

`test_list_nodes_shows_none_identity_when_never_resolved`:
```python
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
```

`test_list_nodes_shows_login_after_resolve_identity`:
```python
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
```

`test_different_public_keys_with_same_node_id_do_not_collide` needs no change — it only extracts `n["fingerprint"]`, never compares a full dict.

Add these new tests at the end of the file:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: FAIL — the five updated assertions fail on the missing `"reputation"` key, and `AttributeError: 'NodeRegistry' object has no attribute 'record_completion'` (and similar) for the six new tests.

- [ ] **Step 3: Implement the changes**

In `src/mycelium/coordinator/registry.py`, add this dataclass right after the `Node` dataclass (before `MissingGithubToken`):

```python
@dataclass
class ReputationCounters:
    # Incremented at the exact points router.NodeTimeoutError /
    # router.NodeError / router.NodeDisconnectedError are already raised
    # (#10/#11), plus a successful completion. Never reset by
    # register()/unregister() — see the design doc for issue #36 on why
    # this must survive a node's disconnect/reconnect cycle to mean
    # anything (the disconnect counter especially: the connection that
    # triggers it is the one about to be unregistered).
    completions: int = 0
    timeouts: int = 0
    crashes: int = 0
    disconnects: int = 0
```

Add this one new line to `NodeRegistry.__init__` (right after the existing `self._identity_verifier = identity_verifier or github_identity.verify_identity` line, still inside `__init__`, no other line in `__init__` changes in this task — the `random_source` constructor parameter itself is Task 2's job, since Task 2 is what first consumes it):

```python
        # public_key -> reputation counters, in-memory only, surviving
        # independently of _nodes — see the design doc for issue #36.
        self._reputation: dict[str, ReputationCounters] = {}
```

Add these four methods right after `unregister` (before `find_node_for_model`):

```python
    def record_completion(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).completions += 1

    def record_timeout(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).timeouts += 1

    def record_crash(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).crashes += 1

    def record_disconnect(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).disconnects += 1
```

Replace `list_nodes`:

```python
    def list_nodes(self) -> list[dict]:
        return [
            {
                "node_id": n.node_id,
                "model": n.model,
                "fingerprint": crypto.fingerprint(n.public_key),
                "identity": (
                    self._identity_by_key[n.public_key].login
                    if n.public_key in self._identity_by_key
                    else None
                ),
                "reputation": _reputation_dict(self._reputation.get(n.public_key)),
            }
            for n in self._nodes.values()
        ]
```

Add this module-level helper function right before the `NodeRegistry` class definition:

```python
def _reputation_dict(counters: ReputationCounters | None) -> dict:
    counters = counters or ReputationCounters()
    return {
        "completions": counters.completions,
        "timeouts": counters.timeouts,
        "crashes": counters.crashes,
        "disconnects": counters.disconnects,
    }
```

(`register`, `unregister`, `find_node_for_model`, `get`, `check_token`, `resolve_identity` are all otherwise unchanged in this task — `find_node_for_model`'s own changes are Task 2's job.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: PASS (all tests, including the 6 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: NodeRegistry reputation counters (issue #36)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Reputation-weighted selection in `find_node_for_model`

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Modify: `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: `NodeRegistry._reputation`, `ReputationCounters` (Task 1).
- Produces: `NodeRegistry.__init__` gains `random_source: random.Random | None = None`; `NodeRegistry.find_node_for_model`'s behavior extended (signature unchanged) — round-robin when candidate weights tie, weighted-random draw otherwise.

- [ ] **Step 1: Write the failing tests**

Add `import random` to the top of `tests/coordinator/test_registry.py` (with the other imports).

Add these tests at the end of the file:

```python
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
```

**Note on the two seeded statistical tests** (`test_find_node_for_model_prefers_more_reliable_node_when_weights_differ`, `test_find_node_for_model_never_fully_excludes_unreliable_node`): `random.Random(42)`/`random.Random(7)` make these fully deterministic — the same seed always produces the same draw sequence in CPython, so there's no run-to-run flakiness. But the *specific* seed values above were chosen without executing the code, so when you implement this, actually run these two tests: if `a_share` doesn't clear `0.7` with seed 42, or if seed 7 doesn't happen to produce at least one `PUBKEY_B` pick within 300 draws, try a different seed (e.g. increment it) or raise the draw count — this is expected tuning against a real formula/RNG pairing, not a sign of a bug in the approach itself.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'random_source'`, and the equal-reputation round-robin test fails because reputation isn't consulted yet (though its assertion happens to match today's plain round-robin output already, so it may pass vacuously before the `random_source` tests fail collection — the important thing is the whole file fails to collect/run cleanly until this task's implementation lands).

- [ ] **Step 3: Implement the changes**

In `src/mycelium/coordinator/registry.py`, add this import to the top of the file (with the other stdlib imports):

```python
import random
```

Add a `random_source` parameter to `NodeRegistry.__init__`'s signature, right after `identity_verifier`:

```python
    def __init__(
        self,
        token: str,
        identity_verifier: Callable[[str], Awaitable[github_identity.GithubIdentity]] | None = None,
        random_source: random.Random | None = None,
    ) -> None:
```

And add this line at the end of `__init__`'s body (after the `self._reputation: dict[str, ReputationCounters] = {}` line Task 1 added):

```python
        # A private instance, never the random module's shared global
        # functions — see the design doc for issue #36 on why (nothing
        # else in the process can perturb this registry's weighted draws
        # by calling random.seed() elsewhere). Tests inject
        # random.Random(<fixed seed>) for reproducible draws.
        self._random = random_source or random.Random()
```

(no other line in `__init__` changes — this is purely additive: one new parameter, one new attribute assignment.)

Add this private method right after `record_disconnect` (before `find_node_for_model`):

```python
    def _reputation_weight(self, public_key: str) -> float:
        """Laplace-smoothed success rate for public_key — 1.0 for a node
        with no recorded history (never penalizes the unproven), never
        exactly 0 no matter how many failures accumulate. See the design
        doc for issue #36."""
        counters = self._reputation.get(public_key)
        if counters is None:
            return 1.0
        total = counters.completions + counters.timeouts + counters.crashes + counters.disconnects
        return (counters.completions + 1) / (total + 1)
```

Replace `find_node_for_model` in full:

```python
    def find_node_for_model(
        self, model: str, exclude: frozenset[str] = frozenset()
    ) -> Node | None:
        """Return a registered node hosting `model`, weighted by recent
        reliability, or None if none match — see the design doc for
        issue #11 (the original round-robin mechanism) and issue #36
        (the weighting pass added on top of it).

        Rotation state is the public_key this returned last time for
        `model`; if every current candidate has an identical reputation
        weight (true whenever none of them have recorded history yet —
        every case the original round-robin behavior was built and
        tested against), the next call returns the candidate after that
        one, wrapping around, exactly as before. If that node has since
        left the registry (or is itself excluded), rotation restarts
        from the front of the current candidate list — no fairness
        guarantee across registry churn, only "don't always pick the
        same node when several are equally healthy."

        When weights genuinely differ, a weighted-random draw (via the
        injected random_source) replaces that deterministic step — still
        capable of picking a less-reliable node, just less often. Either
        way, `_last_returned` is updated to whichever node was actually
        picked, so the rotation continues meaningfully from wherever a
        weighted draw landed.

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

        weights = [self._reputation_weight(node.public_key) for node in candidates]

        if len(set(weights)) == 1:
            start = 0
            last = self._last_returned.get(model)
            if last is not None:
                keys = [node.public_key for node in candidates]
                if last in keys:
                    start = (keys.index(last) + 1) % len(candidates)
            node = candidates[start]
        else:
            node = self._random.choices(candidates, weights=weights, k=1)[0]

        if not exclude:
            self._last_returned[model] = node.public_key
        return node
```

(this replaces the entire method body — the candidate-filtering list comprehension and the final `if not exclude: ...` block are unchanged from today, only the middle "which candidate" logic gains the weight-tie branch.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: PASS (all tests — the full file, including every pre-existing round-robin test, which must still pass unchanged since they all construct zero-history candidates)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: reputation-weighted selection in find_node_for_model (issue #36)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Coordinator records reputation events during routing

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Modify: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `NodeRegistry.record_completion/record_timeout/record_crash/record_disconnect` (Task 1); `router.NodeTimeoutError`, `router.NodeError` (already exist, imported via the existing `from mycelium.coordinator import github_identity, router`).
- Produces: `_handle_complete_request`'s external behavior (message contract, timing, failover) is unchanged — only internal counter bookkeeping and exception-handling structure change.

**Note:** this task also fixes every `list_nodes()`-shaped exact-dict assertion already broken by Task 1's `"reputation"` field addition — `tests/coordinator/test_status_cli.py` is still broken after this task (Task 4's job).

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_server.py`, update these exact-dict `list_nodes()`/`response["nodes"]` assertions to add `"reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0}` (all-zero — none of these five tests' scenarios trigger a routed completion):

`test_registered_node_appears_in_status_query`:
```python
                assert response == {
                    "type": "status",
                    "nodes": [{
                        "node_id": "node-a",
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                        "fingerprint": crypto.fingerprint(public_key),
                        "identity": "octocat",
                        "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
                    }],
                }
```

`test_duplicate_public_key_replaces_and_closes_old_connection` (final assertion):
```python
                assert response["nodes"] == [
                    {
                        "node_id": "node-a",
                        "model": "model-b",
                        "fingerprint": crypto.fingerprint(public_key),
                        "identity": "octocat",
                        "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
                    }
                ]
```

`test_reregistration_with_non_canonical_public_key_spelling_is_treated_as_same_node` (final assertion):
```python
                    assert response["nodes"] == [
                        {
                            "node_id": "node-a",
                            "model": "model-b",
                            "fingerprint": crypto.fingerprint(canonical_public_key),
                            "identity": "octocat",
                            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
                        }
                    ]
```

`test_silently_unresponsive_node_is_dropped_within_ping_timeout_window` (its intermediate status-query assertion, before the liveness window — NOT the final `response["nodes"] == []` one):
```python
            assert response == {
                "type": "status",
                "nodes": [{
                    "node_id": "node-a",
                    "model": "m",
                    "fingerprint": crypto.fingerprint(public_key),
                    "identity": "octocat",
                    "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
                }],
            }
```

**`test_complete_request_client_disconnect_before_reply_does_not_crash_server` needs a NON-zero value** — read this test carefully before editing: the node's delayed reply *does* eventually resolve `route_request` successfully (the client having already disconnected doesn't stop the node's own reply from arriving), so `_handle_complete_request`'s success path runs and calls `record_completion` even though the client never sees the result. Its final assertion becomes:
```python
            assert response["nodes"] == [
                {
                    "node_id": "node-a",
                    "model": "m",
                    "fingerprint": crypto.fingerprint(public_key),
                    "identity": "octocat",
                    "reputation": {"completions": 1, "timeouts": 0, "crashes": 0, "disconnects": 0},
                }
            ]
```

**The two direct-`NodeRegistry`-call tests need real (non-zero, scenario-driven) values, not just the new key added** — these bypass the server's websocket layer but still call the real `server._handle_complete_request` function directly, so its new `record_*` calls fire exactly as they would in production:

`test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead`'s final assertion — node-b's completion succeeds, so it now shows one recorded completion (node-a's dead connection is fully removed from the registry by `unregister`, same as before — no reputation entry to show since it's gone from `_nodes` entirely):
```python
    assert registry.list_nodes() == [
        {
            "node_id": "node-b",
            "model": "m",
            "fingerprint": hashlib.sha256(b"b" * 32).hexdigest()[:12],
            "identity": None,
            "reputation": {"completions": 1, "timeouts": 0, "crashes": 0, "disconnects": 0},
        }
    ]
```

`test_complete_request_does_not_fail_over_on_timeout`'s final assertion — node-a timed out (now recorded), node-b was never contacted (stays all-zero):
```python
    assert registry.list_nodes() == [
        {
            "node_id": "node-a", "model": "m",
            "fingerprint": hashlib.sha256(b"a" * 32).hexdigest()[:12],
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 1, "crashes": 0, "disconnects": 0},
        },
        {
            "node_id": "node-b", "model": "m",
            "fingerprint": hashlib.sha256(b"b" * 32).hexdigest()[:12],
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
        },
    ]
```

`test_complete_request_returns_error_when_every_node_is_dead` needs no change — both nodes end up fully unregistered either way, so its `registry.list_nodes() == []` assertion is unaffected.

**New tests** — add after `test_complete_request_node_reports_failure_is_relayed_to_client`:

```python
async def test_complete_request_success_increments_completion_counter(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_result", "text": f"echo: {msg['prompt']}"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                await client_ws.recv()

            node_task.cancel()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"][0]["reputation"] == {
                    "completions": 1, "timeouts": 0, "crashes": 0, "disconnects": 0,
                }


async def test_complete_request_node_failure_increments_crash_counter(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_error", "reason": "vLLM exploded"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                await client_ws.recv()

            node_task.cancel()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"][0]["reputation"]["crashes"] == 1
```

Add this test after `test_complete_request_node_disconnect_mid_request_fails_fast`:

```python
async def test_complete_request_timeout_increments_timeout_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 0.2)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()

            async def receive_then_never_reply():
                await node_ws.recv()

            node_task = asyncio.create_task(receive_then_never_reply())

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                await client_ws.recv()

            await node_task

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"][0]["reputation"]["timeouts"] == 1
```

Add this test after `test_superseded_node_connection_fails_only_its_own_pending_requests`:

```python
async def test_complete_request_disconnect_increments_disconnect_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, public_key = _register_payload("node-a", "m")
        await node_ws.send(json.dumps(payload))
        await node_ws.recv()

        async def receive_then_never_reply():
            await node_ws.recv()

        node_task = asyncio.create_task(receive_then_never_reply())

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
            ))
            await node_task  # the node has received the routed request
            await node_ws.close()  # simulate the node dying mid-request
            await client_ws.recv()

        # The disconnected connection's Node entry is gone from _nodes
        # (issue #11's self-heal), so list_nodes() won't show it directly.
        # Confirm the counter survived by re-registering the SAME key —
        # if record_disconnect ran, the count carries into this fresh
        # connection (Task 1's persistence-across-reconnect property).
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as reconnect_ws:
            await reconnect_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": payload["public_key"], "signature": payload["signature"],
            }))
            await reconnect_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"][0]["reputation"]["disconnects"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_server.py -v`
Expected: FAIL — every real-registration test's exact-dict assertion fails on the missing `"reputation"` key, and the new tests fail since nothing calls `record_*` yet.

- [ ] **Step 3: Implement the changes**

In `src/mycelium/coordinator/server.py`, replace `_handle_complete_request` in full:

```python
async def _handle_complete_request(websocket, registry: NodeRegistry, message: dict) -> None:
    """A client's one-shot completion request: authenticate, pick a
    healthy node hosting the requested model, forward, relay the result
    (or a clear error) back, then close — see the design doc for issue
    #10. If the picked node turns out to be disconnected, self-heal the
    registry and retry a different healthy node before giving up — see
    the design doc for issue #11. A timeout or a node-reported failure is
    not retried: the node might still be working, and silently re-running
    the same prompt on a second node risks double-executing it. Each
    outcome (completion/timeout/crash/disconnect) is recorded against the
    node that produced it — see the design doc for issue #36."""
    if not registry.check_token(message.get("token")):
        await websocket.close()
        return

    model = message.get("model")
    prompt = message.get("prompt")
    if not model or not prompt:
        try:
            await websocket.send(json.dumps(
                {"type": "complete_error", "reason": "model and prompt are required"}
            ))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.close()
        return

    async def reject(reason: str) -> None:
        try:
            await websocket.send(json.dumps({"type": "complete_error", "reason": reason}))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.close()

    tried: set[str] = set()
    while True:
        try:
            node = registry.find_node_for_model(model, exclude=frozenset(tried))
            if node is None:
                raise router.NoHealthyNodeError(f"no healthy node for model {model!r}")
            # Passed explicitly (not relying on route_request's own default)
            # so tests can monkeypatch router.NODE_COMPLETE_TIMEOUT_SECONDS
            # and have it actually take effect: Python binds a default
            # argument value once at function-definition time, so the
            # default alone would never see a post-import monkeypatch.
            # Production behavior is unchanged — the constant is never
            # mutated after import there.
            text = await router.route_request(
                node, prompt, timeout=router.NODE_COMPLETE_TIMEOUT_SECONDS
            )
        except router.NodeDisconnectedError:
            # The picked node is actually dead — self-heal the registry
            # right away (don't wait for #9's ping/pong timeout) and try a
            # different healthy node instead of failing the request.
            registry.record_disconnect(node.public_key)
            registry.unregister(node.public_key, node.websocket)
            tried.add(node.public_key)
            continue
        except router.NodeTimeoutError as exc:
            registry.record_timeout(node.public_key)
            await reject(str(exc))
            return
        except router.NodeError as exc:
            registry.record_crash(node.public_key)
            await reject(str(exc))
            return
        except router.RoutingError as exc:
            await reject(str(exc))
            return
        else:
            registry.record_completion(node.public_key)
            break

    try:
        await websocket.send(json.dumps({"type": "complete_result", "text": text}))
    except websockets.exceptions.ConnectionClosed:
        return
    await websocket.close()
```

(the token check, model/prompt validation block, and the final result-send block are all unchanged from today — only the failover loop's exception handling and the new `reject` closure change.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_server.py -v`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: coordinator records reputation events during routing (issue #36)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: `mycelium-coordinator-status` shows reputation, docs, full-suite verification

**Files:**
- Modify: `src/mycelium/coordinator/status_cli.py`
- Modify: `tests/coordinator/test_status_cli.py`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/superpowers/specs/2026-08-24-issue-36-reputation-weighted-selection-design.md` (status only)

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_status_cli.py`, update `test_query_status_returns_registered_node`'s final assertion to add the new key:

```python
    assert nodes == [
        {
            "node_id": "node-a",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "fingerprint": crypto.fingerprint(public_key),
            "identity": "octocat",
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
        }
    ]
```

Update `test_main_prints_bound_github_identity` to include non-zero reputation values (proving real rendering, not just a default-shaped pass) — both the fake response dict and the expected output string change:

```python
def test_main_prints_bound_github_identity(tmp_path, monkeypatch, capsys):
    """See Finding 3 of the final whole-branch review for issue #39:
    list_nodes() already returns an "identity" field, but main()'s print
    loop never displayed it. query_status() is faked here so this test
    exercises main()'s own output formatting, not the network path
    (already covered by test_query_status_returns_registered_node)."""
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")

    async def fake_query_status(coordinator_url, coordinator_cert, token):
        return [
            {
                "node_id": "node-a",
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "fingerprint": "a1b2c3d4e5f6",
                "identity": "octocat",
                "reputation": {"completions": 12, "timeouts": 1, "crashes": 0, "disconnects": 2},
            }
        ]

    monkeypatch.setattr(status_cli, "query_status", fake_query_status)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mycelium-coordinator-status",
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ],
    )

    status_cli.main()

    out = capsys.readouterr().out
    assert out == (
        "node-a [a1b2c3d4e5f6] (github:octocat) [ok:12 timeout:1 crash:0 disconnect:2]: "
        "Qwen/Qwen2.5-7B-Instruct\n"
    )
```

Update `test_main_omits_identity_suffix_when_node_has_none` (add the `"reputation"` key to the fake response and the reputation bracket to the expected output — the identity-omission behavior itself is unaffected):

```python
def test_main_omits_identity_suffix_when_node_has_none(tmp_path, monkeypatch, capsys):
    """A node registered without identity resolution (only possible via a
    direct NodeRegistry.register() call, never in production) has
    identity: None — main() must not print a bogus "(github:None)"."""
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")

    async def fake_query_status(coordinator_url, coordinator_cert, token):
        return [
            {
                "node_id": "node-a",
                "model": "m",
                "fingerprint": "a1b2c3d4e5f6",
                "identity": None,
                "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
            }
        ]

    monkeypatch.setattr(status_cli, "query_status", fake_query_status)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mycelium-coordinator-status",
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ],
    )

    status_cli.main()

    out = capsys.readouterr().out
    assert out == "node-a [a1b2c3d4e5f6] [ok:0 timeout:0 crash:0 disconnect:0]: m\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_status_cli.py -v`
Expected: FAIL — all three updated tests fail on the missing reputation bracket in `main()`'s printed output (and `test_query_status_returns_registered_node` fails on the missing dict key, though that one was already failing since Task 1 landed).

- [ ] **Step 3: Implement the changes**

In `src/mycelium/coordinator/status_cli.py`, replace the `for node in nodes:` loop inside `main()`:

```python
    for node in nodes:
        identity = f" (github:{node['identity']})" if node.get("identity") else ""
        rep = node["reputation"]
        reputation = (
            f" [ok:{rep['completions']} timeout:{rep['timeouts']} "
            f"crash:{rep['crashes']} disconnect:{rep['disconnects']}]"
        )
        print(f"{node['node_id']} [{node['fingerprint']}]{identity}{reputation}: {node['model']}")
```

(only this loop body changes — everything else in `main()`, `parse_args`, and `query_status` is untouched.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_status_cli.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Update `docs/OPERATIONS.md`**

In the "Step 4 — Check what's registered" section, update the sample output block:

```markdown
```
your-hostname [a1b2c3d4e5f6] (github:octocat) [ok:12 timeout:1 crash:0 disconnect:2]: Qwen/Qwen2.5-7B-Instruct
```
```

(replacing the existing single-line sample, which currently ends at `): Qwen/Qwen2.5-7B-Instruct` with no reputation bracket — no other text in this section changes.)

- [ ] **Step 6: Mark the design doc implemented**

In `docs/superpowers/specs/2026-08-24-issue-36-reputation-weighted-selection-design.md`, change line 4 from:

```
Status: Approved, not yet implemented
```

to:

```
Status: Implemented
```

- [ ] **Step 7: Commit**

```bash
git add src/mycelium/coordinator/status_cli.py tests/coordinator/test_status_cli.py docs/OPERATIONS.md docs/superpowers/specs/2026-08-24-issue-36-reputation-weighted-selection-design.md
git commit -m "feat: mycelium-coordinator-status shows reputation counters (issue #36)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, zero failures
