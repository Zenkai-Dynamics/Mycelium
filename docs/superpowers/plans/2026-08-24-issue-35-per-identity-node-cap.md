# Issue #35 — Per-Identity Node Cap (Sybil Resistance) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The coordinator rejects a node registration once its bound GitHub identity already has `per_identity_cap` (default 3) other public keys currently registered, with a distinct `registration_rejected` reason — closing the "one signup registers 500 fake nodes" gap #39's identity binding alone doesn't prevent.

**Architecture:** A new `NodeRegistry.enforce_identity_cap(public_key, identity)` method (in `mycelium/coordinator/registry.py`) counts, via a live scan over `_nodes`, how many currently-registered public keys map to the same `identity.id` (via the existing `_identity_by_key` map from #39) and raises `IdentityCapReached` if a *new* slot would exceed the cap — a `public_key` already occupying a slot is exempt, since that's a reconnect, not a new registration. `server.py`'s `_handle_registration` calls it right after `resolve_identity` succeeds, mapping the exception to a rejection reason. The cap value threads through `NodeRegistry.__init__` → `server.serve(...)` → a new `mycelium-coordinator --per-identity-cap` flag, mirroring exactly how #39 threaded `identity_verifier` through the same three layers.

**Tech Stack:** Python 3.11, no new dependency. Builds directly on #39's `NodeRegistry`/`github_identity` module.

## Global Constraints

- `NodeRegistry.enforce_identity_cap(self, public_key: str, identity: github_identity.GithubIdentity) -> None` — raises `IdentityCapReached` (a new, plain `Exception` subclass, matching `MissingGithubToken`'s style) when the cap would be exceeded by a *new* slot; returns normally (no-op) otherwise.
- Cap counting is a live scan over `_nodes`, filtered by `_identity_by_key[node.public_key].id == identity.id` — no new reverse-index data structure, no changes to `register()`/`unregister()`.
- A `public_key` already present in `_nodes` is exempt from the cap check entirely (checked first, before counting).
- `NodeRegistry.__init__` gains `per_identity_cap: int = 3` (an ordinary literal default — not the `None`-sentinel-with-`or` pattern `identity_verifier` uses, since `0` is a legitimate, meaningful cap value that `x or 3` would incorrectly replace with `3`).
- `server.serve(...)` gains `per_identity_cap: int | None = None`, passed straight through to `NodeRegistry(..., per_identity_cap=per_identity_cap)` — `NodeRegistry.__init__` itself resolves `None` to `3` via `per_identity_cap if per_identity_cap is not None else 3` (explicit `is not None`, not `or`, for the same reason).
- `mycelium-coordinator` gains `--per-identity-cap` (`type=int, default=3`), passed to `server.serve(...)`.
- Rejection reason: exactly the `IdentityCapReached` exception's own message, `f"identity has reached the maximum of {cap} registered nodes"` — `_handle_registration` uses `str(exc)` rather than a second hardcoded copy of the string, since server.py has no other way to know the configured cap value.
- Reuses the existing `registration_rejected` message type — no new message type.
- Full design rationale: [docs/superpowers/specs/2026-08-24-issue-35-per-identity-node-cap-design.md](../specs/2026-08-24-issue-35-per-identity-node-cap-design.md).

---

## Task 1: `NodeRegistry.enforce_identity_cap`

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Modify: `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: `github_identity.GithubIdentity` (has `.id`, `.login`) — already imported in this file.
- Produces: `IdentityCapReached(Exception)`; `NodeRegistry.__init__(self, token: str, identity_verifier=None, per_identity_cap: int = 3)`; `NodeRegistry.enforce_identity_cap(self, public_key: str, identity: github_identity.GithubIdentity) -> None`.

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_registry.py`, add a fourth public-key constant near the top (with `PUBKEY_A`/`PUBKEY_B`/`PUBKEY_C`):

```python
PUBKEY_D = base64.b64encode(b"d" * 32).decode()
```

Update the import line to add `IdentityCapReached`:

```python
from mycelium.coordinator.registry import IdentityCapReached, MissingGithubToken, NodeRegistry
```

Add these tests at the end of the file:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'IdentityCapReached'`, and `TypeError: __init__() got an unexpected keyword argument 'per_identity_cap'`.

- [ ] **Step 3: Implement the changes**

In `src/mycelium/coordinator/registry.py`, add this exception class right after `MissingGithubToken`:

```python
class IdentityCapReached(Exception):
    """Raised by NodeRegistry.enforce_identity_cap when registering
    public_key as a NEW slot would push its identity's currently-
    registered node count to or past the configured per-identity cap.
    See the design doc for issue #35."""
```

Replace `NodeRegistry.__init__`:

```python
    def __init__(
        self,
        token: str,
        identity_verifier: Callable[[str], Awaitable[github_identity.GithubIdentity]] | None = None,
        per_identity_cap: int = 3,
    ) -> None:
        if not token:
            raise ValueError("token must not be empty")
        self._token = token
        self._nodes: dict[str, Node] = {}  # keyed by public_key
        # model -> public_key this returned last, for round-robin
        # selection in find_node_for_model. See the design doc for issue
        # #11 (mechanism) and issue #33 (keyed by public_key, not node_id).
        self._last_returned: dict[str, str] = {}
        # public_key -> the GithubIdentity bound to it, in-memory only —
        # see the design doc for issue #39 on why this deliberately does
        # not survive a coordinator restart.
        self._identity_by_key: dict[str, github_identity.GithubIdentity] = {}
        self._identity_verifier = identity_verifier or github_identity.verify_identity
        # Sybil-resistance cap — see the design doc for issue #35. A
        # literal default (not the None-sentinel-with-`or` pattern
        # identity_verifier uses above): 0 is a legitimate, meaningful
        # cap value ("freeze new registrations for every identity"),
        # which `0 or 3` would silently replace with 3.
        self._per_identity_cap = per_identity_cap
```

Add this method right after `resolve_identity` (before `register`):

```python
    def enforce_identity_cap(
        self, public_key: str, identity: github_identity.GithubIdentity
    ) -> None:
        """Raise IdentityCapReached if registering public_key as a NEW
        slot would push identity's currently-registered node count to or
        past the configured cap. A public_key already present in _nodes
        is exempt — it's a reconnect/replace of an existing slot (handled
        by register()'s own supersede semantics), not a new one, so it
        must never be blocked here. Counts live registry occupancy (via
        _identity_by_key), not identity binding history — a disconnected
        node's old key doesn't count against the cap. See the design doc
        for issue #35."""
        if public_key in self._nodes:
            return
        current_count = sum(
            1
            for node in self._nodes.values()
            if (bound := self._identity_by_key.get(node.public_key)) is not None
            and bound.id == identity.id
        )
        if current_count >= self._per_identity_cap:
            raise IdentityCapReached(
                f"identity has reached the maximum of {self._per_identity_cap} registered nodes"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: PASS (all tests, including the 7 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: NodeRegistry.enforce_identity_cap (issue #35)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Coordinator registration enforces the per-identity cap

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Modify: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `NodeRegistry.enforce_identity_cap(public_key, identity)` and `IdentityCapReached` (Task 1).
- Produces: `serve(host, port, cert_path, key_path, token, identity_verifier=None, per_identity_cap=None)` — new trailing optional parameter. `registration_rejected` gains a fourth possible reason: `"identity has reached the maximum of {cap} registered nodes"`.

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_server.py`, add these three tests after `test_registration_reconnect_with_known_key_needs_no_github_token`:

```python
async def test_registration_rejected_when_identity_cap_reached(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, per_identity_cap=1,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as first_ws:
            payload, _ = _register_payload("node-a", "m")
            await first_ws.send(json.dumps(payload))
            response = json.loads(await first_ws.recv())
            assert response == {"type": "registered"}

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
                # A DIFFERENT key, same fake identity ("octocat") — the
                # cap of 1 is already occupied by the first connection.
                payload, _ = _register_payload("node-b", "m")
                await second_ws.send(json.dumps(payload))
                response = json.loads(await second_ws.recv())
                assert response == {
                    "type": "registration_rejected",
                    "reason": "identity has reached the maximum of 1 registered nodes",
                }
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await second_ws.recv()


async def test_registration_succeeds_after_identity_cap_slot_freed_by_disconnect(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, per_identity_cap=1,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        first_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, _ = _register_payload("node-a", "m")
        await first_ws.send(json.dumps(payload))
        await first_ws.recv()

        await first_ws.close()
        await asyncio.sleep(0.2)  # let the coordinator notice the disconnect

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
            payload, _ = _register_payload("node-b", "m")
            await second_ws.send(json.dumps(payload))
            response = json.loads(await second_ws.recv())
            assert response == {"type": "registered"}


async def test_registration_reconnect_of_existing_key_not_blocked_by_identity_cap(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, per_identity_cap=1,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        private_key = crypto.generate_keypair()
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as first_ws:
            await first_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
                "github_token": "valid-github-token",
            }))
            await first_ws.recv()

        # Reconnect with the SAME key — already occupies the (only) cap
        # slot, so this must succeed even though the identity is nominally
        # "at" its cap of 1.
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
            await second_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
            }))
            response = json.loads(await second_ws.recv())
            assert response == {"type": "registered"}
```

No existing test in this file needs any change: every existing test registers at most 2 distinct public keys under the shared fake identity at once, and the default cap is 3 — none of them cross it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_server.py -v`
Expected: FAIL — the three new tests error with `TypeError: serve() got an unexpected keyword argument 'per_identity_cap'`.

- [ ] **Step 3: Implement the changes**

In `src/mycelium/coordinator/server.py`, update the import:

```python
from mycelium.coordinator.registry import IdentityCapReached, MissingGithubToken, Node, NodeRegistry
```

In `_handle_registration`, capture `resolve_identity`'s return value and add the cap check right after it (replace this block):

```python
    try:
        await registry.resolve_identity(public_key, message.get("github_token"))
    except MissingGithubToken:
```

with:

```python
    try:
        identity = await registry.resolve_identity(public_key, message.get("github_token"))
    except MissingGithubToken:
```

(only the assignment target changes — the three `except` clauses below it, through the last `return`, are untouched). Then add the cap check immediately after that whole `try`/`except` block, before `superseded = registry.register(...)`:

```python
    try:
        registry.enforce_identity_cap(public_key, identity)
    except IdentityCapReached as exc:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": str(exc),
        }))
        await websocket.close()
        return

    superseded = registry.register(public_key, node_id, model, websocket)
```

Replace `serve`'s signature (body is unchanged except the `NodeRegistry(...)` call):

```python
def serve(
    host: str,
    port: int,
    cert_path: Path,
    key_path: Path,
    token: str,
    identity_verifier: Callable[[str], Awaitable] | None = None,
    per_identity_cap: int | None = None,
):
    """Start the coordinator's node-facing WebSocket server.

    identity_verifier overrides the real GitHub identity check (see
    mycelium.coordinator.github_identity.verify_identity) — production
    callers leave it unset; tests inject a fake so no test ever makes a
    real network call to GitHub. See the design doc for issue #39.

    per_identity_cap overrides NodeRegistry's default Sybil-resistance
    cap (3) — production callers leave it unset unless the operator
    configured a different value via mycelium-coordinator's
    --per-identity-cap flag. See the design doc for issue #35. Resolved
    to the literal default here (not passed through as None) because
    NodeRegistry.__init__ takes an ordinary `int = 3` default, not a
    None-accepting parameter — `None >= 3` would raise inside
    enforce_identity_cap if passed through unconditionally.

    Returns whatever `websockets.serve` returns: awaitable to get a `Server`
    instance directly, or usable as `async with serve(...) as server:`.
    """
    ssl_context = build_ssl_context(cert_path, key_path)
    registry = NodeRegistry(
        token,
        identity_verifier=identity_verifier,
        per_identity_cap=per_identity_cap if per_identity_cap is not None else 3,
    )
```

(everything else in `serve()` — the `handler` closure, the `websockets.serve(...)` call — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_server.py -v`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: coordinator registration enforces the per-identity node cap (issue #35)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: `mycelium-coordinator --per-identity-cap`

**Files:**
- Modify: `src/mycelium/coordinator/cli.py`
- Modify: `tests/coordinator/test_cli.py`

**Interfaces:**
- Consumes: `server.serve(host, port, cert_path, key_path, token, per_identity_cap=None)` (Task 2).
- Produces: `parse_args` gains `.per_identity_cap: int` (default `3`).

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_cli.py`, add these two tests after `test_parse_args_overrides`:

```python
def test_parse_args_per_identity_cap_default(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    args = parse_args(["--token-file", str(token_file)])
    assert args.per_identity_cap == 3


def test_parse_args_per_identity_cap_override(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    args = parse_args(
        ["--token-file", str(token_file), "--per-identity-cap", "5"]
    )
    assert args.per_identity_cap == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_cli.py -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'per_identity_cap'`

- [ ] **Step 3: Implement the changes**

In `src/mycelium/coordinator/cli.py`, add the new argument to `parse_args` (after `--token-file`, before `--cert-san-ip`):

```python
    parser.add_argument("--per-identity-cap", type=int, default=3)
```

Update `_run`'s `server.serve(...)` call:

```python
    async with server.serve(
        args.host, args.port, args.cert_file, args.key_file, token,
        per_identity_cap=args.per_identity_cap,
    ):
        await asyncio.Future()  # run forever
```

(only this call changes — everything else in `_run` and `main` is untouched.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_cli.py -v`
Expected: PASS (all tests, including the 2 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/cli.py tests/coordinator/test_cli.py
git commit -m "feat: mycelium-coordinator --per-identity-cap flag (issue #35)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Documentation and full-suite verification

**Files:**
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/superpowers/specs/2026-08-24-issue-35-per-identity-node-cap-design.md` (status only)

- [ ] **Step 1: Update `docs/OPERATIONS.md` Step 2**

In the "Step 2 — Start the coordinator" section, add a new bullet after the existing "Default cert/key paths..." bullet:

```markdown
- Per-identity node cap (issue #35): each bound GitHub identity can have
  at most 3 nodes registered at once by default — override with
  `--per-identity-cap`. A registration beyond the cap is rejected with a
  clear reason distinct from an invalid GitHub token; a node
  disconnecting frees its slot for that identity immediately.
```

- [ ] **Step 2: Mark the design doc implemented**

In `docs/superpowers/specs/2026-08-24-issue-35-per-identity-node-cap-design.md`, change line 4 from:

```
Status: Approved, not yet implemented
```

to:

```
Status: Implemented
```

- [ ] **Step 3: Commit**

```bash
git add docs/OPERATIONS.md docs/superpowers/specs/2026-08-24-issue-35-per-identity-node-cap-design.md
git commit -m "docs: document --per-identity-cap, mark issue #35 design implemented

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, zero failures
