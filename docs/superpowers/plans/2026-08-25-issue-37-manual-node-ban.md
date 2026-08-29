# Issue #37 — Manual Node Ban (Operator Override) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator a `mycelium-coordinator-ban` command that revokes a GitHub identity outright — rejecting all its future registration attempts and disconnecting any of its currently-registered nodes immediately.

**Architecture:** `NodeRegistry` gains an in-memory set of banned GitHub identity ids plus two methods (`enforce_not_banned`, `ban_identity`) following the exact injectable/exception-based pattern already established by `enforce_identity_cap`/`IdentityCapReached` (#35). `server.py` wires `enforce_not_banned` into `_handle_registration`'s existing check chain (right after identity resolution, before the cap check) and adds a new `ban_identity` wire message + handler that reuses the existing `_close_in_background` cleanup path rather than duplicating disconnect logic. A new CLI, `mycelium-coordinator-ban`, is a thin clone of `mycelium-coordinator-status`'s connection pattern.

**Tech Stack:** Python 3.11, `websockets`, `pytest`/`pytest-asyncio` (see existing coordinator modules — no new dependencies).

## Global Constraints

- Ban target is the GitHub `login` string (what `mycelium-coordinator-status` already displays as `(github:<login>)`) — `--identity <login>` on the CLI. Internally this is resolved to the stable numeric `GithubIdentity.id` and it's the `id`, not the `login`, that's actually recorded as banned.
- No unban command and no pre-emptive ban of a login the coordinator has never seen — `ban_identity` raises `UnknownIdentity` in that case. Ban state is in-memory only; a coordinator restart is the only reset path (matches every other piece of coordinator state: identity bindings, the per-identity cap, reputation counters).
- `NodeRegistry.ban_identity()` identifies and returns the affected `Node` objects — it never calls `unregister()` itself. `server.py` closes each returned node's websocket via the existing `_close_in_background(...)` helper, and that connection's own long-lived handler's existing `finally` block does the actual `unregister` + pending-future cleanup, exactly as it already does for a superseded connection.
- Check order in `_handle_registration`: required-fields → signature → canonicalize key → `resolve_identity` → `enforce_not_banned` → `enforce_identity_cap` → `register`. Ban is checked before the cap.
- Unlike `enforce_identity_cap`, `enforce_not_banned` has **no** exemption for an already-registered public key — a banned identity's reconnect using its existing key must still be rejected.
- New exceptions `UnknownIdentity` and `IdentityBanned` follow the existing plain-`Exception`-subclass, docstring-explained style already used for `MissingGithubToken`/`IdentityCapReached` — no new pattern.
- Registration-time rejection reason string (reuses the existing `registration_rejected` message type, unchanged): exactly `"this identity has been banned by the operator"`.
- New wire message pair for the ban command itself: `{"type": "ban_identity", "token": ..., "identity": "<login>"}` → `{"type": "banned", "identity": ..., "disconnected_count": N}` on success, or `{"type": "ban_failed", "reason": ...}` on failure (unknown identity). Gated by the exact same shared operator token via `registry.check_token`, same as `status_query`.
- `mycelium-coordinator-ban` reuses `mycelium-coordinator-status`'s exact `--coordinator-url`/`--coordinator-cert`/`--token-file` flags and one-shot connect/send/receive/close shape, adding only `--identity`.
- No changes to `mycelium-coordinator-status`'s output, no unban/appeal mechanism, no persistence across restart, no coordinator startup flag for this feature (it's a separate one-shot command, not a `serve()` parameter) — all explicitly out of scope per the design doc.

---

### Task 1: `NodeRegistry` — ban state, `enforce_not_banned`, `ban_identity`

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Test: `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: `github_identity.GithubIdentity` (fields `id: str`, `login: str`), existing `self._nodes: dict[str, Node]`, existing `self._identity_by_key: dict[str, GithubIdentity]`.
- Produces: `class UnknownIdentity(Exception)`, `class IdentityBanned(Exception)`, `NodeRegistry.enforce_not_banned(identity: github_identity.GithubIdentity) -> None` (raises `IdentityBanned`), `NodeRegistry.ban_identity(login: str) -> list[Node]` (raises `UnknownIdentity`). These are consumed directly by Task 2.

- [ ] **Step 1: Write the failing tests**

Append to `tests/coordinator/test_registry.py`:

```python
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
```

Also update the existing import line near the top of the file from:

```python
from mycelium.coordinator.registry import IdentityCapReached, MissingGithubToken, NodeRegistry
```

to:

```python
from mycelium.coordinator.registry import (
    IdentityBanned,
    IdentityCapReached,
    MissingGithubToken,
    NodeRegistry,
    UnknownIdentity,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_registry.py -k "ban" -v`
Expected: FAIL with `ImportError` (`IdentityBanned`/`UnknownIdentity` don't exist yet) or `AttributeError: 'NodeRegistry' object has no attribute 'enforce_not_banned'`/`'ban_identity'`.

- [ ] **Step 3: Implement**

In `src/mycelium/coordinator/registry.py`, add the two new exception classes right after `IdentityCapReached`:

```python
class UnknownIdentity(Exception):
    """Raised by NodeRegistry.ban_identity when the operator asks to ban
    a GitHub login the coordinator has never seen bind to any public key.
    See the design doc for issue #37."""


class IdentityBanned(Exception):
    """Raised by NodeRegistry.enforce_not_banned when a registration
    attempt's resolved identity has been banned by the operator. See the
    design doc for issue #37."""
```

In `NodeRegistry.__init__`, add the banned-ids set right after the `self._per_identity_cap = per_identity_cap` assignment (and its preceding comment block), before the reputation dict is initialized:

```python
        # GithubIdentity.id values banned by the operator, in-memory only
        # — see the design doc for issue #37 on why there's no unban
        # command (a coordinator restart is the only reset path). Keyed
        # on the stable numeric id, never the login: a login is what the
        # operator types, but it isn't immutable across GitHub username
        # renames the way id is.
        self._banned_identity_ids: set[str] = set()
```

Add `enforce_not_banned` right before `enforce_identity_cap` (so the class's method order matches the check order the design doc mandates):

```python
    def enforce_not_banned(self, identity: github_identity.GithubIdentity) -> None:
        """Raise IdentityBanned if identity has been banned by the
        operator. Unlike enforce_identity_cap, there is NO exemption for
        a public_key that's already registered — a banned identity's
        reconnect attempts using an existing key must be rejected too.
        See the design doc for issue #37."""
        if identity.id in self._banned_identity_ids:
            raise IdentityBanned("this identity has been banned by the operator")
```

Add `ban_identity` right after `enforce_identity_cap`:

```python
    def ban_identity(self, login: str) -> list[Node]:
        """Ban the identity currently bound to GitHub login `login` and
        return the list of currently-registered Node objects under that
        identity, so the caller (server.py) can disconnect them. Does NOT
        unregister them itself — see the design doc for issue #37 on why
        that's server.py's job, reusing the existing superseded-connection
        cleanup path rather than duplicating it. Raises UnknownIdentity if
        the coordinator has never seen `login` bind to any public key.
        Resolves by scanning currently-known identity bindings rather than
        keeping a separate login index — bindings are already the sole
        source of truth for "what identities has this coordinator seen,"
        and there's no volume concern at this scale."""
        identity = next(
            (bound for bound in self._identity_by_key.values() if bound.login == login),
            None,
        )
        if identity is None:
            raise UnknownIdentity(f"no known identity bound to GitHub login {login!r}")
        self._banned_identity_ids.add(identity.id)
        return [
            node
            for node in self._nodes.values()
            if (bound := self._identity_by_key.get(node.public_key)) is not None
            and bound.id == identity.id
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_registry.py -v`
Expected: PASS (all tests in the file, including the 8 new ones and every pre-existing test — this task only adds new state/methods, it doesn't touch any existing code path).

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: add NodeRegistry.ban_identity and enforce_not_banned (issue #37)"
```

---

### Task 2: `server.py` — wire the ban check into registration and add the ban wire message

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Test: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes (from Task 1): `registry.enforce_not_banned(identity: GithubIdentity) -> None` raising `IdentityBanned`; `registry.ban_identity(login: str) -> list[Node]` raising `UnknownIdentity`. Consumes the existing `_close_in_background(websocket) -> None` helper (unchanged) and the existing `registry.check_token(token) -> bool`.
- Produces: `_handle_ban_request(websocket, registry, message) -> None`; a new `"ban_identity"` branch in `_handle_node`'s dispatch; `_handle_registration` now rejects a banned identity's registration with reason `"this identity has been banned by the operator"` before the identity-cap check runs.

- [ ] **Step 1: Write the failing tests**

Append to `tests/coordinator/test_server.py`:

```python
async def test_registration_rejected_when_identity_is_banned(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as first_ws:
            payload, _ = _register_payload("node-a", "m")
            await first_ws.send(json.dumps(payload))
            response = json.loads(await first_ws.recv())
            assert response == {"type": "registered"}

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ban_ws:
            await ban_ws.send(json.dumps(
                {"type": "ban_identity", "token": "secret-token", "identity": "octocat"}
            ))
            await ban_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
            payload, _ = _register_payload("node-b", "m")
            await second_ws.send(json.dumps(payload))
            response = json.loads(await second_ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "this identity has been banned by the operator",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await second_ws.recv()


async def test_ban_disconnects_currently_registered_node(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, _ = _register_payload("node-a", "m")
        await node_ws.send(json.dumps(payload))
        await node_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ban_ws:
            await ban_ws.send(json.dumps(
                {"type": "ban_identity", "token": "secret-token", "identity": "octocat"}
            ))
            response = json.loads(await ban_ws.recv())
            assert response == {
                "type": "banned", "identity": "octocat", "disconnected_count": 1,
            }

        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await node_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []


async def test_ban_request_with_wrong_token_is_closed_without_reply(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "ban_identity", "token": "wrong", "identity": "octocat"}
            ))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_ban_request_for_unknown_identity_returns_ban_failed(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "ban_identity", "token": "secret-token", "identity": "nobody"}
            ))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "ban_failed",
                "reason": "no known identity bound to GitHub login 'nobody'",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_ban_is_checked_before_identity_cap(tmp_path):
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
            await first_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ban_ws:
                await ban_ws.send(json.dumps(
                    {"type": "ban_identity", "token": "secret-token", "identity": "octocat"}
                ))
                await ban_ws.recv()

            # first_ws is still open here, so the identity is simultaneously
            # AT its cap of 1 and banned — a NEW key for that same identity
            # must see the ban rejection, not the cap rejection. If ban
            # weren't checked first, this would instead see
            # "identity has reached the maximum of 1 registered nodes".
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
                payload, _ = _register_payload("node-b", "m")
                await second_ws.send(json.dumps(payload))
                response = json.loads(await second_ws.recv())
                assert response == {
                    "type": "registration_rejected",
                    "reason": "this identity has been banned by the operator",
                }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_server.py -k "ban" -v`
Expected: FAIL — `test_registration_rejected_when_identity_is_banned` and `test_ban_is_checked_before_identity_cap` fail because the `ban_identity` message they send gets no reply (connection closes as an unrecognized message type, so `.recv()` inside the `ban_ws`/`first_ws` context raises `ConnectionClosed` instead of returning JSON); the other three fail the same way when they call `.recv()` expecting a `banned`/`ban_failed` response.

- [ ] **Step 3: Implement**

In `src/mycelium/coordinator/server.py`, update the registry import:

```python
from mycelium.coordinator.registry import (
    IdentityBanned,
    IdentityCapReached,
    MissingGithubToken,
    Node,
    NodeRegistry,
    UnknownIdentity,
)
```

Add a new dispatch branch in `_handle_node`, right after the `"register"` branch and before the `"complete"` branch:

```python
    if message_type == "ban_identity":
        await _handle_ban_request(websocket, registry, message)
        return
```

Add the new handler function. Place it right after `_handle_status_query` (both are one-shot operator-token-gated queries/commands):

```python
async def _handle_ban_request(websocket, registry: NodeRegistry, message: dict) -> None:
    """An operator's `mycelium-coordinator-ban` command: authenticate,
    ban the named GitHub login, then disconnect any currently-registered
    nodes under that identity — see the design doc for issue #37.
    Disconnecting reuses the same _close_in_background(...) path
    _handle_registration already uses for a superseded connection; each
    disconnected node's own long-lived handler (this same function,
    running for that node's original connection) notices
    ConnectionClosed and runs its existing finally-block cleanup
    (registry.unregister + failing any pending routed requests)."""
    if not registry.check_token(message.get("token")):
        await websocket.close()
        return

    identity = message.get("identity")
    if not identity:
        await websocket.send(json.dumps(
            {"type": "ban_failed", "reason": "identity is required"}
        ))
        await websocket.close()
        return

    try:
        disconnected_nodes = registry.ban_identity(identity)
    except UnknownIdentity as exc:
        await websocket.send(json.dumps({"type": "ban_failed", "reason": str(exc)}))
        await websocket.close()
        return

    for node in disconnected_nodes:
        _close_in_background(node.websocket)

    await websocket.send(json.dumps({
        "type": "banned", "identity": identity, "disconnected_count": len(disconnected_nodes),
    }))
    await websocket.close()
```

Finally, insert the ban check into `_handle_registration`, right after the `resolve_identity` try/except block and before the existing `enforce_identity_cap` try/except block:

```python
    try:
        registry.enforce_not_banned(identity)
    except IdentityBanned as exc:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": str(exc),
        }))
        await websocket.close()
        return

```

(This goes immediately before the existing `try: registry.enforce_identity_cap(...)` block — the surrounding code is otherwise unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_server.py -v`
Expected: PASS (all tests in the file, including the 5 new ones and every pre-existing test).

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: reject banned identities at registration, add ban_identity wire message (issue #37)"
```

---

### Task 3: `mycelium-coordinator-ban` CLI

**Files:**
- Create: `src/mycelium/coordinator/ban_cli.py`
- Test: `tests/coordinator/test_ban_cli.py`
- Modify: `pyproject.toml`
- Modify: `docs/OPERATIONS.md`

**Interfaces:**
- Consumes (from Task 2): the wire messages `{"type": "ban_identity", "token": ..., "identity": ...}` → `{"type": "banned", "identity": ..., "disconnected_count": N}` / `{"type": "ban_failed", "reason": ...}`. Consumes `mycelium.node.connection.build_ssl_context(cert_path) -> ssl.SSLContext` (same helper `status_cli.py` already uses).
- Produces: `mycelium.coordinator.ban_cli.parse_args(argv=None) -> argparse.Namespace` (fields `coordinator_url`, `coordinator_cert`, `token_file`, `identity`); `async def ban_identity(coordinator_url: str, coordinator_cert: Path, token: str, identity: str, timeout: float = BAN_REQUEST_TIMEOUT_SECONDS) -> int` (returns `disconnected_count`, raises `BanError`); `class BanError(Exception)`; `def main() -> None`. Console script `mycelium-coordinator-ban`.

- [ ] **Step 1: Write the failing tests**

Create `tests/coordinator/test_ban_cli.py`:

```python
"""Tests for mycelium.coordinator.ban_cli."""

import ssl
import sys

import pytest
import websockets

from mycelium import crypto
from mycelium.coordinator import ban_cli, certs, server
from mycelium.coordinator import github_identity
from mycelium.coordinator.ban_cli import BanError, ban_identity, parse_args
from mycelium.node import registration


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")


def test_parse_args_requires_all_flags():
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_valid(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    args = parse_args(
        [
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--identity", "octocat",
        ]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == str(cert_path)
    assert str(args.token_file) == str(token_file)
    assert args.identity == "octocat"


async def test_ban_identity_disconnects_registered_node(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            private_key = crypto.generate_keypair()
            public_key = crypto.public_key_b64(private_key)
            await registration.register(
                node_ws, model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a",
                public_key=public_key, signature=crypto.sign_public_key(private_key),
                github_token="valid-github-token",
            )

            disconnected_count = await ban_identity(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "octocat"
            )

    assert disconnected_count == 1


async def test_ban_identity_raises_on_unknown_identity(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(BanError, match="no known identity"):
            await ban_identity(f"wss://127.0.0.1:{port}", cert_path, "secret-token", "nobody")


async def test_ban_identity_raises_on_wrong_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(BanError, match="rejected"):
            await ban_identity(f"wss://127.0.0.1:{port}", cert_path, "wrong-token", "octocat")


def test_main_prints_success_message(tmp_path, monkeypatch, capsys):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")

    async def fake_ban_identity(coordinator_url, coordinator_cert, token, identity):
        return 2

    monkeypatch.setattr(ban_cli, "ban_identity", fake_ban_identity)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mycelium-coordinator-ban",
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--identity", "octocat",
        ],
    )

    ban_cli.main()

    out = capsys.readouterr().out
    assert out == "banned 'octocat' — disconnected 2 currently-registered node(s)\n"


def test_main_prints_error_and_exits_1_on_ban_error(tmp_path, monkeypatch, capsys):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    token_file = tmp_path / "token"
    token_file.write_text("secret")

    async def fake_ban_identity(coordinator_url, coordinator_cert, token, identity):
        raise BanError("no known identity bound to GitHub login 'nobody'")

    monkeypatch.setattr(ban_cli, "ban_identity", fake_ban_identity)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mycelium-coordinator-ban",
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--identity", "nobody",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        ban_cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert out == "error: no known identity bound to GitHub login 'nobody'\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_ban_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.coordinator.ban_cli'`.

- [ ] **Step 3: Implement**

Create `src/mycelium/coordinator/ban_cli.py`:

```python
"""CLI entry point for banning a node's bound GitHub identity.

See the design doc for issue #37. Connects the same way
mycelium-coordinator-status does (same TLS cert, same shared token) as a
one-shot request/response — this is a manual operator override, not a
node agent. There is no unban command: ban state is in-memory only, like
every other piece of coordinator state, so a coordinator restart is the
only reset path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

from mycelium.node.connection import build_ssl_context

BAN_REQUEST_TIMEOUT_SECONDS = 10.0


class BanError(Exception):
    """Raised when the ban command fails: the coordinator rejected it
    (wrong/missing token, unknown identity) or didn't respond in time."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-coordinator-ban")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--identity", required=True)
    return parser.parse_args(argv)


async def ban_identity(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    identity: str,
    timeout: float = BAN_REQUEST_TIMEOUT_SECONDS,
) -> int:
    """Connect, ask the coordinator to ban `identity`, and return the
    number of currently-connected nodes it disconnected as a result.

    Raises BanError if the coordinator rejects the command (a wrong
    token — it closes without replying; an unknown identity — it replies
    with ban_failed) or doesn't respond in time."""
    ssl_context = build_ssl_context(coordinator_cert)
    async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
        await websocket.send(json.dumps(
            {"type": "ban_identity", "token": token, "identity": identity}
        ))
        try:
            async with asyncio.timeout(timeout):
                raw = await websocket.recv()
        except TimeoutError:
            raise BanError(f"coordinator did not respond within {timeout}s") from None
        except websockets.exceptions.ConnectionClosed:
            raise BanError(
                "coordinator rejected the ban command (check --token-file)"
            ) from None
        message = json.loads(raw)
        if message.get("type") == "ban_failed":
            raise BanError(message.get("reason", "ban failed"))
        return message.get("disconnected_count", 0)


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    try:
        disconnected_count = asyncio.run(
            ban_identity(args.coordinator_url, args.coordinator_cert, token, args.identity)
        )
    except BanError as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    print(
        f"banned {args.identity!r} — disconnected {disconnected_count} "
        f"currently-registered node(s)"
    )


if __name__ == "__main__":
    main()
```

Add the console script entry to `pyproject.toml`'s `[project.scripts]` section, right after `mycelium-coordinator-status`:

```toml
[project.scripts]
mycelium-node = "mycelium.node.cli:main"
mycelium-coordinator = "mycelium.coordinator.cli:main"
mycelium-coordinator-status = "mycelium.coordinator.status_cli:main"
mycelium-coordinator-ban = "mycelium.coordinator.ban_cli:main"
mycelium-client = "mycelium.client.cli:main"
```

Add a new section to `docs/OPERATIONS.md`, right after the existing "## Step 5 — Send a completion as a client" section (before "## Troubleshooting"). The whole section (including its own nested code fences) is shown below wrapped in a 4-backtick fence purely so it displays correctly in *this plan document* — when you write it into `OPERATIONS.md`, copy only the inner content (the `## Step 6 ...` heading through the closing sentence), using ordinary 3-backtick fences for its two code blocks exactly as shown:

````markdown
## Step 6 — Ban a misbehaving identity (operator override)

If a volunteer's node needs to be removed for cause — e.g. a report of
bad-faith output that never tripped a timeout or crash counter — revoke
their bound GitHub identity outright:

```bash
mycelium-coordinator-ban \
  --coordinator-url wss://<coordinator-ip>:8765 \
  --coordinator-cert ~/.mycelium/coordinator-cert.pem \
  --token-file ~/.mycelium/token \
  --identity <github-login>
```

Use the login shown by `mycelium-coordinator-status`'s `(github:<login>)`
suffix. On success:

```
banned 'octocat' — disconnected 1 currently-registered node(s)
```

Every currently-registered node under that identity is disconnected
immediately, and every future registration attempt from it is rejected
with `this identity has been banned by the operator` — reconnects using
an already-registered key included. There's no unban command: ban state
is in-memory only, like every other piece of coordinator state, so a
coordinator restart is the only way to reverse a ban. Banning a GitHub
login the coordinator has never seen bind to any node fails instead:
`error: no known identity bound to GitHub login '<login>'`.
````

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_ban_cli.py -v`
Expected: PASS (all 7 tests).

Then run the full suite to confirm nothing else broke:

Run: `pytest -v`
Expected: PASS (every test in the repo, including all of Tasks 1 and 2's new tests).

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/ban_cli.py tests/coordinator/test_ban_cli.py pyproject.toml docs/OPERATIONS.md
git commit -m "feat: add mycelium-coordinator-ban CLI (issue #37)"
```
