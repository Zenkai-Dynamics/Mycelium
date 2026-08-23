# Issue #39 — GitHub OAuth Identity Verification Gates Node Registration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Phase 0's shared-token gate for node registration with real GitHub-based trust: a node's first-ever registration under a given public key must carry a valid GitHub OAuth token, verified once against `GET /user`; the coordinator remembers the resulting identity for that public key for as long as it keeps running, so every later reconnect needs neither the token nor a fresh GitHub call. Client-side auth is completely untouched.

**Architecture:** A new `mycelium/coordinator/github_identity.py` owns the real GitHub call (stdlib `urllib.request` in a thread) and the `GithubIdentity`/exception types. `NodeRegistry` gains one new method, `resolve_identity`, that encapsulates "already bound → return it; unbound → verify and bind" and is injectable for tests. `server.py`'s `_handle_registration` calls it in place of the old `check_token` call and maps its three failure modes to three distinct `registration_rejected` reasons. `node/registration.py` and `node/cli.py` carry the new optional `github_token` field end to end, replacing `--token-file` with `--github-token-file`.

**Tech Stack:** Python 3.11, stdlib `urllib.request` (no new dependency), `cryptography==50.0.0`, `websockets==17.0.1`, `pytest`/`pytest-asyncio`.

## Global Constraints

- `register` drops the `token` field entirely (client `complete`/`status_query` keep using the shared token, untouched). It gains an optional `github_token` field.
- Identity check order in `_handle_registration`: required-fields (`node_id`/`model`) → `public_key`/`signature` required → signature valid → canonicalize key → identity check. The identity check replaces where the old token check used to sit (first).
- A public key with an already-bound identity: `resolve_identity` returns it immediately, ignoring any `github_token` sent — never re-verified, no GitHub call.
- A public key with no bound identity: missing `github_token` → reject `"github_token is required for first-time registration"`. Present but rejected by GitHub (401/403) → reject `"invalid or expired GitHub token"`. Present but GitHub unreachable/timeout/unexpected response → reject `"could not reach GitHub to verify identity, try again"`. All three reuse the existing `registration_rejected` message type.
- `verify_identity(github_token) -> GithubIdentity` (fields: `id`, `login`), real implementation in `mycelium/coordinator/github_identity.py`, using `urllib.request` wrapped in `asyncio.to_thread`, 5-second timeout (`github_identity.VERIFY_TIMEOUT_SECONDS`).
- `registration.REGISTRATION_TIMEOUT_SECONDS`: 10.0 → 15.0. `server.FIRST_MESSAGE_TIMEOUT_SECONDS`: 10.0 → 15.0 (bumped together to preserve the existing `test_server_and_registration_agree_on_timeout_settings` invariant — not because the two constants are logically coupled).
- Persistence: in-memory only (`NodeRegistry`'s own dict) — no durable store, matching every other piece of coordinator state.
- `mycelium-node` CLI: `--token-file` removed, `--github-token-file` added (optional, same read/strip/empty-check pattern). No dual-mode/backward-compat shim.
- `mycelium-coordinator-status` shows a bound identity's `login` (not the raw numeric `id`) as `"identity"`; `None` if a node's identity was never resolved (only possible via direct `NodeRegistry.register()` calls in tests — every real registration path always resolves identity first).
- Full design rationale: [docs/superpowers/specs/2026-08-23-issue-39-github-oauth-identity-verification-design.md](../specs/2026-08-23-issue-39-github-oauth-identity-verification-design.md).

---

## Task 1: GitHub identity verification (`mycelium/coordinator/github_identity.py`)

**Files:**
- Create: `src/mycelium/coordinator/github_identity.py`
- Test: `tests/coordinator/test_github_identity.py`

**Interfaces:**
- Produces: `GithubIdentity(id: str, login: str)` (frozen dataclass); `IdentityVerificationError(Exception)`; `InvalidGithubToken(IdentityVerificationError)`; `GithubUnreachable(IdentityVerificationError)`; `GITHUB_USER_URL: str`; `VERIFY_TIMEOUT_SECONDS: float = 5.0`; `async def verify_identity(github_token: str) -> GithubIdentity`.

- [ ] **Step 1: Write the failing tests**

Create `tests/coordinator/test_github_identity.py`:

```python
"""Tests for mycelium.coordinator.github_identity."""

import json
import urllib.error
import urllib.request

from mycelium.coordinator import github_identity


class _FakeResponse:
    """Stand-in for the context-manager urlopen() returns."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


async def test_verify_identity_returns_id_and_login_on_success(monkeypatch):
    def fake_urlopen(request, timeout):
        assert request.full_url == github_identity.GITHUB_USER_URL
        assert request.get_header("Authorization") == "Bearer good-token"
        return _FakeResponse({"id": 12345, "login": "octocat"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    identity = await github_identity.verify_identity("good-token")

    assert identity == github_identity.GithubIdentity(id="12345", login="octocat")


async def test_verify_identity_passes_the_configured_timeout(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        return _FakeResponse({"id": 1, "login": "x"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    await github_identity.verify_identity("token")

    assert captured["timeout"] == github_identity.VERIFY_TIMEOUT_SECONDS


async def test_verify_identity_raises_invalid_token_on_401(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(github_identity.GITHUB_USER_URL, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("bad-token")
        assert False, "expected InvalidGithubToken"
    except github_identity.InvalidGithubToken:
        pass


async def test_verify_identity_raises_invalid_token_on_403(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(github_identity.GITHUB_USER_URL, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("bad-token")
        assert False, "expected InvalidGithubToken"
    except github_identity.InvalidGithubToken:
        pass


async def test_verify_identity_raises_unreachable_on_server_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(github_identity.GITHUB_USER_URL, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("any-token")
        assert False, "expected GithubUnreachable"
    except github_identity.GithubUnreachable:
        pass


async def test_verify_identity_raises_unreachable_on_network_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("any-token")
        assert False, "expected GithubUnreachable"
    except github_identity.GithubUnreachable:
        pass


async def test_verify_identity_raises_unreachable_on_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("any-token")
        assert False, "expected GithubUnreachable"
    except github_identity.GithubUnreachable:
        pass


async def test_verify_identity_raises_unreachable_on_malformed_response(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"unexpected": "shape"})  # no id/login

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("any-token")
        assert False, "expected GithubUnreachable"
    except github_identity.GithubUnreachable:
        pass


async def test_invalid_github_token_and_github_unreachable_are_both_identity_verification_errors():
    assert issubclass(github_identity.InvalidGithubToken, github_identity.IdentityVerificationError)
    assert issubclass(github_identity.GithubUnreachable, github_identity.IdentityVerificationError)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_github_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.coordinator.github_identity'`

- [ ] **Step 3: Write the implementation**

Create `src/mycelium/coordinator/github_identity.py`:

```python
"""Real GitHub identity verification for node registration.

Called by NodeRegistry.resolve_identity (see coordinator/registry.py) the
first time a node's public key registers — never on a reconnect. See the
design doc for issue #39 for the full rationale, including why a real
401/403 from GitHub (the volunteer's problem) gets a different rejection
reason than every other failure mode (the coordinator's/network's
problem). Tests never call this module's real verify_identity — they
inject a fake identity_verifier into NodeRegistry instead, so no test
ever makes a real network call to GitHub.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

GITHUB_USER_URL = "https://api.github.com/user"

# Bounds the blocking GET /user call — see the design doc for issue #39.
# This is also why registration.REGISTRATION_TIMEOUT_SECONDS and
# server.FIRST_MESSAGE_TIMEOUT_SECONDS were bumped to 15.0: comfortable
# headroom over this call's worst case plus existing overhead.
VERIFY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class GithubIdentity:
    # GitHub's stable numeric user id (as a string) — immutable across
    # username renames. This, not login, is what #35's cap and #37's ban
    # will index on.
    id: str
    # The GitHub username at verification time — display-only (see
    # NodeRegistry.list_nodes), can go stale if the volunteer renames
    # later. A cosmetic concern only, since nothing keys off it.
    login: str


class IdentityVerificationError(Exception):
    """Base class: verify_identity failed for any reason."""


class InvalidGithubToken(IdentityVerificationError):
    """GitHub itself rejected the token (401/403) — the volunteer's
    problem, not the coordinator's."""


class GithubUnreachable(IdentityVerificationError):
    """Timeout, network error, or an unexpected status/response shape —
    the coordinator's/network's problem, not the token's. Deliberately
    distinct from InvalidGithubToken so a transient GitHub outage isn't
    reported to a volunteer as "your token is bad"."""


def _fetch_user(github_token: str) -> GithubIdentity:
    """Blocking GET /user call — only ever run via asyncio.to_thread,
    never called directly from an async context."""
    request = urllib.request.Request(
        GITHUB_USER_URL,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "mycelium-coordinator",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=VERIFY_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise InvalidGithubToken(f"GitHub rejected the token: HTTP {exc.code}") from exc
        raise GithubUnreachable(f"unexpected GitHub response: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GithubUnreachable(f"could not reach GitHub: {exc}") from exc

    try:
        return GithubIdentity(id=str(body["id"]), login=body["login"])
    except (KeyError, TypeError) as exc:
        raise GithubUnreachable(f"unexpected GitHub response shape: {body!r}") from exc


async def verify_identity(github_token: str) -> GithubIdentity:
    """Verify github_token against GitHub's GET /user and return the
    resulting identity. Raises InvalidGithubToken for a real 401/403,
    GithubUnreachable for anything else. Runs the blocking call in a
    thread so it never blocks the coordinator's event loop — the same
    pattern node/cli.py uses for vLLM's start/wait_ready."""
    return await asyncio.to_thread(_fetch_user, github_token)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_github_identity.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/github_identity.py tests/coordinator/test_github_identity.py
git commit -m "feat: GitHub identity verification via GET /user (issue #39)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: `NodeRegistry.resolve_identity`

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Modify: `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: `mycelium.coordinator.github_identity.{GithubIdentity, IdentityVerificationError, InvalidGithubToken, verify_identity}` (Task 1)
- Produces: `MissingGithubToken(Exception)`; `NodeRegistry.__init__(self, token: str, identity_verifier: Callable[[str], Awaitable[GithubIdentity]] | None = None)`; `async def NodeRegistry.resolve_identity(self, public_key: str, github_token: str | None) -> GithubIdentity`; `NodeRegistry.list_nodes()` dicts gain an `"identity"` key (the bound `login`, or `None` if never resolved). `register`/`unregister`/`get`/`find_node_for_model`/`check_token` are unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_registry.py`, add this import near the top (after the existing `from mycelium.coordinator.registry import NodeRegistry` line):

```python
from mycelium.coordinator import github_identity
from mycelium.coordinator.registry import MissingGithubToken, NodeRegistry
```

(replacing the single existing `from mycelium.coordinator.registry import NodeRegistry` line with the two lines above).

Add this fake verifier near the top of the file, after the `PUBKEY_*`/`_expected_fingerprint` definitions:

```python
async def _fake_verifier(github_token: str) -> github_identity.GithubIdentity:
    if github_token == "bad-token":
        raise github_identity.InvalidGithubToken("bad token")
    return github_identity.GithubIdentity(id="42", login="octocat")
```

Update these three existing tests' `list_nodes()` assertions to add `"identity": None` (these tests never call `resolve_identity`, so the bound identity stays unresolved):

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
        }
    ]
```

`test_different_public_keys_with_same_node_id_do_not_collide` needs no change — it only extracts `n["fingerprint"]`, never compares a full dict.

Add these new tests at the end of the file:

```python
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
        }
    ]


def test_node_registry_without_identity_verifier_still_constructs():
    # No identity_verifier given — must default internally, not raise or
    # require the caller to know about identity verification at all.
    registry = NodeRegistry("secret")
    registry.register(PUBKEY_A, "node-a", "model-a", websocket="ws-a")
    assert registry.get(PUBKEY_A) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'MissingGithubToken'`, plus the three updated `list_nodes()` assertions failing on the missing `"identity"` key.

- [ ] **Step 3: Implement the changes**

In `src/mycelium/coordinator/registry.py`, change the imports at the top:

```python
from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mycelium import crypto
from mycelium.coordinator import github_identity
```

Add this exception class right before the `NodeRegistry` class definition (after the `Node` dataclass):

```python
class MissingGithubToken(Exception):
    """Raised by NodeRegistry.resolve_identity when public_key has no
    bound identity yet and no github_token was supplied to establish
    one. See the design doc for issue #39."""
```

Replace `NodeRegistry.__init__`:

```python
    def __init__(
        self,
        token: str,
        identity_verifier: Callable[[str], Awaitable[github_identity.GithubIdentity]] | None = None,
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
```

Add this method after `check_token` (before `register`):

```python
    async def resolve_identity(
        self, public_key: str, github_token: str | None
    ) -> github_identity.GithubIdentity:
        """Return the GithubIdentity bound to public_key. If public_key
        is already bound (an earlier call this coordinator process has
        seen), returns it immediately and never re-verifies —
        github_token is ignored entirely in that case, even if supplied.
        Otherwise github_token is required: raises MissingGithubToken if
        it's absent/empty, or propagates whatever
        github_identity.IdentityVerificationError subclass the injected
        verifier raises if verification fails. See the design doc for
        issue #39 on why a bound identity is never re-checked: a
        reconnect must not cost a GitHub API call, by design."""
        existing = self._identity_by_key.get(public_key)
        if existing is not None:
            return existing
        if not github_token:
            raise MissingGithubToken("github_token is required for first-time registration")
        identity = await self._identity_verifier(github_token)
        self._identity_by_key[public_key] = identity
        return identity
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
            }
            for n in self._nodes.values()
        ]
```

(`register`, `unregister`, `find_node_for_model`, `get` are unchanged — do not touch them.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_registry.py -v`
Expected: PASS (all tests, including the 9 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: NodeRegistry.resolve_identity binds GitHub identity to a public key (issue #39)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Coordinator registration requires GitHub identity, not a shared token

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Modify (rewrite): `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `NodeRegistry.resolve_identity(public_key, github_token)` and `MissingGithubToken` (Task 2); `github_identity.{InvalidGithubToken, IdentityVerificationError}` (Task 1).
- Produces: `register` no longer requires/uses a `token` field; `serve(host, port, cert_path, key_path, token, identity_verifier=None)` — new optional last parameter, passed straight through to `NodeRegistry`. `registration_rejected` gains three possible reasons in place of the old token-rejection ones: `"github_token is required for first-time registration"`, `"invalid or expired GitHub token"`, `"could not reach GitHub to verify identity, try again"`.

- [ ] **Step 1: Write the failing tests (rewrite of `tests/coordinator/test_server.py`)**

Add this import near the top (with the other `mycelium.coordinator` imports):

```python
from mycelium.coordinator import github_identity
```

Add this fake verifier right after the `_register_payload` helper:

```python
async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    """Accepts any token except the sentinel "bad-github-token" — see the
    design doc for issue #39. Injected into every server.serve(...) call
    in this file so no test ever makes a real network call to GitHub."""
    if github_token == "bad-github-token":
        raise github_identity.InvalidGithubToken("bad token")
    return github_identity.GithubIdentity(id="1", login="octocat")
```

Replace `_register_payload` (drops the `token` field, adds `github_token`, defaulting to a value `_fake_identity_verifier` always accepts):

```python
def _register_payload(
    node_id: str, model: str, github_token: str | None = "valid-github-token"
) -> tuple[dict, str]:
    """Build a valid register message with a freshly generated Ed25519
    keypair. Returns (payload, public_key_b64) so callers can compute the
    expected fingerprint for status-query assertions. github_token
    defaults to a value _fake_identity_verifier always accepts; pass None
    to omit the field entirely (e.g. a reconnect that needs none) or
    "bad-github-token" to test rejection. Reconnect tests that need the
    SAME key across two registrations build their payload dicts directly
    instead of through this helper — it exists to keep the many one-shot,
    fresh-key call sites for other tests short."""
    private_key = crypto.generate_keypair()
    public_key_b64 = crypto.public_key_b64(private_key)
    signature_b64 = crypto.sign_public_key(private_key)
    payload = {
        "type": "register",
        "model": model,
        "node_id": node_id,
        "public_key": public_key_b64,
        "signature": signature_b64,
    }
    if github_token is not None:
        payload["github_token"] = github_token
    return payload, public_key_b64
```

**Global mechanical change:** add `identity_verifier=_fake_identity_verifier` as a keyword argument to every single `server.serve(...)` call in this file (there is no test where adding it does harm — tests that never register a node simply never invoke it). For example:

```python
async with server.serve(
    "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
) as coordinator:
```

**Delete these three tests entirely** — they test node-registration token-rejection behavior that no longer exists (the `token` field is gone from `register`):
- `test_registration_with_invalid_token_is_rejected_and_closed`
- `test_registration_with_missing_token_is_rejected`
- `test_registration_with_null_token_is_rejected_not_crashed`

**Manually-built registration payloads** (tests that don't use `_register_payload` because they need the SAME key across two `send` calls) — add `"github_token": "valid-github-token"` to the FIRST registration dict of each pair (harmless to also add to the second; simplest is to add it to both). Apply to: `test_duplicate_public_key_replaces_and_closes_old_connection`, `test_duplicate_node_id_registration_acks_promptly_even_if_old_connection_is_unresponsive`, `test_superseded_node_connection_fails_only_its_own_pending_requests`. For example, in `test_duplicate_public_key_replaces_and_closes_old_connection`:

```python
        await old_ws.send(json.dumps({
            "type": "register", "model": "model-a", "node_id": "node-a",
            "public_key": public_key, "signature": signature,
            "github_token": "valid-github-token",
        }))
        await old_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_ws:
            await new_ws.send(json.dumps({
                "type": "register", "model": "model-b", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
            }))
```

(the second send omits `github_token` entirely — proving the reconnect-needs-no-token behavior in the same breath as the existing supersede assertion. Its final `nodes ==` assertion also needs `"identity": "octocat"` added, shown below.)

`test_reregistration_with_non_canonical_public_key_spelling_is_treated_as_same_node` — add `"github_token": "valid-github-token"` only to the FIRST send (`ws_first`); the second (`ws_second`, non-canonical spelling of the same raw key) sends none, proving canonicalization happens before the identity check recognizes it as the same key.

**Exact-dict `nodes ==` assertions needing `"identity"` added** (all resolve to `"octocat"`, from `_fake_identity_verifier`'s canned identity, since every registration in this file that reaches this point used a valid `github_token`):
- `test_registered_node_appears_in_status_query`
- `test_duplicate_public_key_replaces_and_closes_old_connection`
- `test_reregistration_with_non_canonical_public_key_spelling_is_treated_as_same_node`
- `test_silently_unresponsive_node_is_dropped_within_ping_timeout_window` (its intermediate status-query assertion, before the liveness window)
- `test_complete_request_client_disconnect_before_reply_does_not_crash_server`

For example, `test_registered_node_appears_in_status_query`'s assertion becomes:

```python
                assert response == {
                    "type": "status",
                    "nodes": [{
                        "node_id": "node-a",
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                        "fingerprint": crypto.fingerprint(public_key),
                        "identity": "octocat",
                    }],
                }
```

**Cosmetic cleanup** — remove the now-meaningless `"token": "secret-token"` key from `test_registration_missing_public_key_or_signature_is_rejected`'s and `test_registration_with_invalid_signature_is_rejected`'s payload dicts (harmless if left, but implies a check that no longer runs — no other change to either test).

**Direct-`NodeRegistry`-call tests** (bottom of the file — `test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead`, `test_complete_request_does_not_fail_over_on_timeout`, `test_complete_request_returns_error_when_every_node_is_dead`) never call `resolve_identity`, so their `list_nodes()` assertions need `"identity": None` added. For example, `test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead`'s final assertion:

```python
    assert registry.list_nodes() == [
        {
            "node_id": "node-b",
            "model": "m",
            "fingerprint": hashlib.sha256(b"b" * 32).hexdigest()[:12],
            "identity": None,
        }
    ]
```

and `test_complete_request_does_not_fail_over_on_timeout`'s final assertion:

```python
    assert registry.list_nodes() == [
        {"node_id": "node-a", "model": "m", "fingerprint": hashlib.sha256(b"a" * 32).hexdigest()[:12], "identity": None},
        {"node_id": "node-b", "model": "m", "fingerprint": hashlib.sha256(b"b" * 32).hexdigest()[:12], "identity": None},
    ]
```

(`test_complete_request_returns_error_when_every_node_is_dead`'s assertion is `registry.list_nodes() == []` — no change needed.)

**New tests** — add after `test_registration_with_invalid_signature_is_rejected`:

```python
async def test_registration_new_key_without_github_token_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            payload, _ = _register_payload("node-a", "m", github_token=None)
            await ws.send(json.dumps(payload))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "github_token is required for first-time registration",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_registration_new_key_with_invalid_github_token_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            payload, _ = _register_payload("node-a", "m", github_token="bad-github-token")
            await ws.send(json.dumps(payload))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "invalid or expired GitHub token",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_registration_reconnect_with_known_key_needs_no_github_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
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
            response = json.loads(await first_ws.recv())
            assert response == {"type": "registered"}

        # Reconnect with the SAME key and NO github_token at all — must
        # still succeed, since the identity is already bound.
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
            await second_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
            }))
            response = json.loads(await second_ws.recv())
            assert response == {"type": "registered"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/coordinator/test_server.py -v`
Expected: FAIL — every real-registration test errors on `identity_verifier` being an unexpected keyword argument to `server.serve(...)`, and the three new/rewritten rejection tests fail since `server.py` doesn't check `github_token` yet.

- [ ] **Step 3: Implement the changes in `server.py`**

Change the imports at the top of `src/mycelium/coordinator/server.py`:

```python
from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

import websockets

from mycelium import crypto
from mycelium.coordinator import github_identity, router
from mycelium.coordinator.registry import MissingGithubToken, Node, NodeRegistry
```

Change the `FIRST_MESSAGE_TIMEOUT_SECONDS` constant and its surrounding comment:

```python
# These three also double as #9's node-liveness mechanism: a silent node
# (no pong within PING_TIMEOUT_SECONDS of a ping) has the library start a
# close handshake it can never complete, so its connection is force-closed
# after CLOSE_TIMEOUT_SECONDS more — which _handle_registration's cleanup
# already turns into a registry drop. See
# test_silently_unresponsive_node_is_dropped_within_ping_timeout_window in
# tests/coordinator/test_server.py. Worst case from "node goes silent" to
# "dropped from the registry": PING_INTERVAL_SECONDS + PING_TIMEOUT_SECONDS
# + CLOSE_TIMEOUT_SECONDS ≈ 50s.
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20
CLOSE_TIMEOUT_SECONDS = 10
# Bumped from 10.0 (issue #39): registration can now involve a GitHub API
# call (NodeRegistry.resolve_identity), bounded at
# github_identity.VERIFY_TIMEOUT_SECONDS (5s). Kept numerically equal to
# registration.REGISTRATION_TIMEOUT_SECONDS by convention (see
# test_server_and_registration_agree_on_timeout_settings in
# tests/test_integration.py) — this constant's own job (bounding time to
# receive the first message at all) doesn't itself depend on GitHub.
FIRST_MESSAGE_TIMEOUT_SECONDS = 15.0
```

Replace `_handle_registration` in full:

```python
async def _handle_registration(websocket, registry: NodeRegistry, message: dict) -> None:
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

    # Collapse non-canonical base64 spellings of the same raw key to one
    # string, so the registry's string-keyed dicts can't be handed the
    # same key twice under different spellings — see
    # crypto.canonical_public_key.
    public_key = crypto.canonical_public_key(public_key)

    try:
        await registry.resolve_identity(public_key, message.get("github_token"))
    except MissingGithubToken:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": "github_token is required for first-time registration",
        }))
        await websocket.close()
        return
    except github_identity.InvalidGithubToken:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": "invalid or expired GitHub token",
        }))
        await websocket.close()
        return
    except github_identity.IdentityVerificationError:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": "could not reach GitHub to verify identity, try again",
        }))
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

(only the removed `check_token` block at the top and the new `try`/`except` block before `registry.register(...)` change — everything else in this function is untouched.)

Replace `serve`'s signature and body:

```python
def serve(
    host: str,
    port: int,
    cert_path: Path,
    key_path: Path,
    token: str,
    identity_verifier: Callable[[str], Awaitable] | None = None,
):
    """Start the coordinator's node-facing WebSocket server.

    identity_verifier overrides the real GitHub identity check (see
    mycelium.coordinator.github_identity.verify_identity) — production
    callers leave it unset; tests inject a fake so no test ever makes a
    real network call to GitHub. See the design doc for issue #39.

    Returns whatever `websockets.serve` returns: awaitable to get a `Server`
    instance directly, or usable as `async with serve(...) as server:`.
    """
    ssl_context = build_ssl_context(cert_path, key_path)
    registry = NodeRegistry(token, identity_verifier=identity_verifier)

    async def handler(websocket):
        await _handle_node(websocket, registry)

    return websockets.serve(
        handler,
        host,
        port,
        ssl=ssl_context,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
        close_timeout=CLOSE_TIMEOUT_SECONDS,
    )
```

Finally, in `_handle_complete_request`'s failover loop, no change is needed — it already keys on `node.public_key` (from issue #33) and never touched the registration path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/coordinator/test_server.py -v`
Expected: PASS (all tests, including the 3 new ones; 3 fewer than before due to the deletions)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: coordinator registration requires GitHub identity, not a shared token (issue #39)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Node registration message carries `github_token`, not `token`

**Files:**
- Modify: `src/mycelium/node/registration.py`
- Modify: `tests/node/test_registration.py`

**Interfaces:**
- Produces: `register(websocket, model: str, node_id: str, public_key: str, signature: str, github_token: str | None = None, timeout: float = REGISTRATION_TIMEOUT_SECONDS) -> None` — raises `RegistrationRejected`/`RegistrationTimeout` exactly as before. `REGISTRATION_TIMEOUT_SECONDS`: 10.0 → 15.0.

- [ ] **Step 1: Update the failing tests**

In `tests/node/test_registration.py`, remove `token="secret"` (or `token="bad"`) from every `registration.register(...)` call across all 8 test functions, and add `github_token=...` where the test cares about it. Specifically:

`test_register_succeeds_and_returns_on_registered_response`:

```python
async def test_register_succeeds_and_returns_on_registered_response(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    received = {}

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await registration.register(
                ws, model="Qwen/Qwen2.5-7B-Instruct", node_id="node-a",
                public_key="pubkey-a", signature="sig-a", github_token="gh-token-1",
            )

    assert received == {
        "type": "register",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "node_id": "node-a",
        "public_key": "pubkey-a",
        "signature": "sig-a",
        "github_token": "gh-token-1",
    }
```

For the remaining 7 tests (`test_register_raises_rejected_on_registration_rejected_response`, `test_register_raises_timeout_when_coordinator_never_responds`, `test_register_raises_on_unexpected_response_type`, `test_register_raises_on_malformed_json_response`, `test_register_raises_on_non_dict_json_response`, `test_register_raises_rejected_when_coordinator_closes_without_responding`), just remove `token="secret"` (or `token="bad"`) from each call — no `github_token` needed, since none of them assert on the sent message. For example:

```python
            with pytest.raises(registration.RegistrationRejected, match="invalid token"):
                await registration.register(ws, model="m", node_id="node-a",
                    public_key="pubkey-a", signature="sig-a")
```

Add two new tests at the end of the file:

```python
async def test_register_omits_github_token_when_not_provided(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    received = {}

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as fake_server:
        port = fake_server.sockets[0].getsockname()[1]
        client_ctx = connection.build_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await registration.register(
                ws, model="m", node_id="node-a", public_key="pubkey-a", signature="sig-a"
            )

    assert "github_token" not in received


def test_registration_timeout_is_15_seconds():
    assert registration.REGISTRATION_TIMEOUT_SECONDS == 15.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/node/test_registration.py -v`
Expected: FAIL with `TypeError: register() got an unexpected keyword argument 'token'` (and the new timeout test failing on `10.0 != 15.0`)

- [ ] **Step 3: Implement the change**

In `src/mycelium/node/registration.py`, change the timeout constant:

```python
# Bumped from 10.0 (issue #39): a first-time registration can now
# involve the coordinator making an outbound GitHub API call, bounded at
# github_identity.VERIFY_TIMEOUT_SECONDS (5s) — see the design doc for
# issue #39. Kept equal to server.FIRST_MESSAGE_TIMEOUT_SECONDS by
# convention (see test_server_and_registration_agree_on_timeout_settings
# in tests/test_integration.py).
REGISTRATION_TIMEOUT_SECONDS = 15.0
```

Replace the `register` function:

```python
async def register(
    websocket,
    model: str,
    node_id: str,
    public_key: str,
    signature: str,
    github_token: str | None = None,
    timeout: float = REGISTRATION_TIMEOUT_SECONDS,
) -> None:
    """Send the registration message and wait for the coordinator's
    response. Returns normally on success. github_token is only needed
    on this public key's first-ever registration — omit it (leave as
    None) on every later reconnect; the coordinator ignores it once the
    key is already bound, so it's also harmless to keep passing it. See
    the design doc for issue #39. Raises RegistrationRejected if the
    coordinator rejects the registration (or closes the connection
    before responding), or RegistrationTimeout if no response arrives in
    time."""
    message = {
        "type": "register",
        "model": model,
        "node_id": node_id,
        "public_key": public_key,
        "signature": signature,
    }
    if github_token is not None:
        message["github_token"] = github_token
    await websocket.send(json.dumps(message))
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

(only the function's parameter list and the `websocket.send(...)` payload change — everything from `try: async with asyncio.timeout(...)` down is untouched.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/node/test_registration.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/registration.py tests/node/test_registration.py
git commit -m "feat: node registration message carries github_token, not token (issue #39)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: `mycelium-node` CLI drops `--token-file`, adds `--github-token-file`

**Files:**
- Modify: `src/mycelium/node/cli.py`
- Modify: `tests/node/test_cli.py`

**Interfaces:**
- Consumes: `registration.register(websocket, model, node_id, public_key, signature, github_token=None, ...)` (Task 4)
- Produces: `parse_args` no longer has `.token_file`; gains `.github_token_file: Path | None` (default `None`, no required-together validation with `--coordinator-url`).

- [ ] **Step 1: Update the failing tests**

In `tests/node/test_cli.py`:

`test_parse_args_prompt_alone_is_valid` — replace the last assertion:

```python
def test_parse_args_prompt_alone_is_valid():
    args = parse_args(["--prompt", "hello"])
    assert args.prompt == "hello"
    assert args.coordinator_url is None
    assert args.coordinator_cert is None
    assert args.github_token_file is None
```

Replace `test_parse_args_coordinator_requires_token_file` entirely with a test of the new (opposite) behavior — `--github-token-file` is optional, so `--coordinator-url`+`--coordinator-cert` alone must now succeed:

```python
def test_parse_args_coordinator_alone_is_valid_without_github_token_file(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    args = parse_args(
        ["--coordinator-url", "wss://example:8765", "--coordinator-cert", str(cert_path)]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert args.github_token_file is None
```

Replace `test_parse_args_coordinator_alone_is_valid` (drops `--token-file`):

```python
def test_parse_args_coordinator_alone_is_valid(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    args = parse_args(
        ["--coordinator-url", "wss://example:8765", "--coordinator-cert", str(cert_path)]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == str(cert_path)
    assert args.prompt is None
```

Add a new test after `test_parse_args_node_key_file_override`:

```python
def test_parse_args_github_token_file_override(tmp_path):
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    github_token_file = tmp_path / "github-token"
    github_token_file.write_text("gh-secret")
    args = parse_args(
        [
            "--coordinator-url", "wss://example:8765",
            "--coordinator-cert", str(cert_path),
            "--github-token-file", str(github_token_file),
        ]
    )
    assert str(args.github_token_file) == str(github_token_file)
```

`test_run_registers_with_coordinator_using_token_and_node_id` — rename and rewrite:

```python
async def test_run_registers_with_coordinator_using_github_token_and_node_id(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    github_token_file = tmp_path / "github-token"
    github_token_file.write_text("gh-secret-token\n")

    received = {}
    registered_event = asyncio.Event()

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))
        registered_event.set()
        await websocket.wait_closed()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--github-token-file", str(github_token_file),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(_run(args, process))
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    from mycelium import crypto

    assert received["type"] == "register"
    assert received["github_token"] == "gh-secret-token"
    assert received["model"] == vllm_process.DEFAULT_MODEL
    assert received["node_id"] == "test-node"
    assert crypto.verify_registration_signature(received["public_key"], received["signature"]) is True


async def test_run_omits_github_token_when_no_github_token_file_given(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    received = {}
    registered_event = asyncio.Event()

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))
        registered_event.set()
        await websocket.wait_closed()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(_run(args, process))
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert "github_token" not in received
```

`test_run_answers_a_routed_complete_request` — replace `"--token-file", str(token_file)` (and its `token_file` creation) with `"--github-token-file", str(github_token_file)` (and a matching `github_token_file = tmp_path / "github-token"; github_token_file.write_text("gh-secret-token\n")`) — no other change to this test.

`test_run_retries_after_registration_rejected`, `test_registration_backoff_resets_after_a_successful_registration`, `test_sigterm_stops_vllm_process_group_with_no_orphans` — these don't assert on the token's value or presence; simply **remove** the `token_file`/`--token-file` lines entirely from each (no replacement needed, since `--github-token-file` is optional).

`test_run_rejects_empty_token_file_before_starting_vllm` — rename and rewrite for the new flag:

```python
async def test_run_rejects_empty_github_token_file_before_starting_vllm(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    github_token_file = tmp_path / "github-token"
    github_token_file.write_text("  \n")

    args = parse_args(
        [
            "--coordinator-url", "wss://127.0.0.1:1",
            "--coordinator-cert", str(cert_path),
            "--github-token-file", str(github_token_file),
            "--vllm-port", str(vllm_port),
        ]
    )
    process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    with pytest.raises(SystemExit, match="empty"):
        await _run(args, process)

    # vLLM must never have been started — the empty-file check happens
    # before process.start(), so there's nothing to clean up here and no
    # subprocess was spawned.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/node/test_cli.py -v`
Expected: FAIL with `AttributeError: 'Namespace' object has no attribute 'github_token_file'` and `SystemExit` where none is expected (the `--token-file`-requiring validation still runs)

- [ ] **Step 3: Implement the changes**

In `src/mycelium/node/cli.py`, replace `parse_args`:

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-node")
    parser.add_argument("--coordinator-url", default=None)
    parser.add_argument("--coordinator-cert", type=Path, default=None)
    parser.add_argument("--github-token-file", type=Path, default=None)
    parser.add_argument("--node-id", default=None)
    parser.add_argument("--node-key-file", type=Path, default=identity.DEFAULT_KEY_PATH)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gpu", default=DEFAULT_GPU)
    parser.add_argument("--vllm-port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--prompt",
        default=None,
        help="Send this one prompt to vLLM and exit, without connecting to a coordinator.",
    )
    args = parser.parse_args(argv)

    has_url = args.coordinator_url is not None
    has_cert = args.coordinator_cert is not None
    if has_url != has_cert:
        parser.error("--coordinator-url and --coordinator-cert must be given together")
    if not (has_url and has_cert) and args.prompt is None:
        parser.error("either --coordinator-url/--coordinator-cert or --prompt is required")

    return args
```

(only the `--token-file` line is replaced with `--github-token-file`, and the `if has_url and args.token_file is None: parser.error(...)` block is removed — `--github-token-file` is never required.)

Replace `_run`'s setup block (the part before `print(f"starting vLLM ...")`):

```python
async def _run(args: argparse.Namespace, process: VLLMProcess) -> None:
    node_id = None
    public_key = None
    signature = None
    github_token = None
    if args.prompt is None:
        node_id = args.node_id or socket.gethostname()
        private_key = identity.load_or_create_keypair(args.node_key_file)
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)
        if args.github_token_file is not None:
            github_token = args.github_token_file.read_text().strip()
            if not github_token:
                raise SystemExit(f"--github-token-file at {args.github_token_file} is empty")
```

And update the `registration.register(...)` call further down:

```python
                await registration.register(
                    websocket, model=args.model, node_id=node_id,
                    public_key=public_key, signature=signature, github_token=github_token,
                )
```

(everything else in `_run` — the vLLM start/wait_ready block, the `async for websocket in connection.connect(...)` loop's structure, the exception handling — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/node/test_cli.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/cli.py tests/node/test_cli.py
git commit -m "feat: mycelium-node drops --token-file, adds --github-token-file (issue #39)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Fix remaining call sites, run the full suite

**Files:**
- Modify: `tests/coordinator/test_status_cli.py`
- Modify: `tests/test_integration.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5.

- [ ] **Step 1: Fix `tests/coordinator/test_status_cli.py`**

Add this import near the top:

```python
from mycelium.coordinator import github_identity
```

Add a fake verifier after the imports:

```python
async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")
```

Update both `server.serve(...)` calls to pass it:

```python
    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
```

Update `test_query_status_returns_registered_node`'s `registration.register(...)` call (drops `token=`, adds `github_token=`) and its final assertion:

```python
async def test_query_status_returns_registered_node(tmp_path):
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

            nodes = await query_status(f"wss://127.0.0.1:{port}", cert_path, "secret-token")

    assert nodes == [
        {
            "node_id": "node-a",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "fingerprint": crypto.fingerprint(public_key),
            "identity": "octocat",
        }
    ]
```

`test_query_status_returns_empty_list_when_no_nodes_registered` and `test_query_status_raises_on_wrong_token` need only the `identity_verifier=_fake_identity_verifier` kwarg added to their `server.serve(...)` calls — no other change (neither registers a node).

Run: `.venv/bin/pytest tests/coordinator/test_status_cli.py -v`
Expected: PASS (5 tests)

- [ ] **Step 2: Fix `tests/test_integration.py`**

Add this import near the top:

```python
from mycelium.coordinator import github_identity
```

Add a fake verifier after the imports:

```python
async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")
```

In `test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop`, update both `server.serve(...)` calls (`coordinator1` and `coordinator2`) to pass `identity_verifier=_fake_identity_verifier`, and update the `registration.register(...)` call:

```python
            await registration.register(
                websocket, model="m", node_id="node-a",
                public_key=public_key, signature=signature, github_token="valid-github-token",
            )
```

In `test_full_round_trip_client_through_coordinator_to_node_and_back`, update the `server.serve(...)` call to pass `identity_verifier=_fake_identity_verifier`, and update the `registration.register(...)` call the same way:

```python
                    await registration.register(
                        websocket, model="m", node_id="node-a",
                        public_key=public_key, signature=signature, github_token="valid-github-token",
                    )
```

`test_server_and_connection_agree_on_keepalive_settings` and `test_server_and_registration_agree_on_timeout_settings` need no changes — the latter still passes (`15.0 == 15.0`) after Tasks 3 and 4's bumps.

Run: `.venv/bin/pytest tests/test_integration.py -v`
Expected: PASS (4 tests)

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, zero failures

- [ ] **Step 4: Commit**

```bash
git add tests/coordinator/test_status_cli.py tests/test_integration.py
git commit -m "test: fix status_cli/integration call sites broken by github_token param (issue #39)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: Documentation

**Files:**
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/superpowers/specs/2026-08-23-issue-39-github-oauth-identity-verification-design.md` (status only)

- [ ] **Step 1: Update `docs/OPERATIONS.md` Step 1**

Replace the "Step 1 — Create a shared token" section's opening paragraph (the `openssl rand -hex 32 ...` code block and its "Copy this same file..." sentence stay as-is; only the prose above them changes):

```markdown
## Step 1 — Create a shared token

Every **client** request (and the coordinator's own bootstrap) authenticates
with the same secret token, compared with `hmac.compare_digest` (not sent
as a CLI flag or environment variable; always a file). **Nodes no longer
use this token** (see Step 3) — as of issue #39, a node's identity is its
own self-generated keypair plus a one-time GitHub sign-in, not a shared
secret. Generate the client/coordinator token and put it somewhere only
you can read:
```

(the sentence after the code block changes from "Copy this same file (or its contents) to every node and every client machine" to:)

```markdown
Copy this same file (or its contents) to every client machine —
`scp ~/.mycelium/token <client-host>:~/.mycelium/token`, etc. Anyone who
has it can submit completions, so treat it like a password.
```

- [ ] **Step 2: Update `docs/OPERATIONS.md` Step 3**

Replace the "Step 3 — Start a node" section's opening paragraph and command (up to but not including the numbered "What happens" list):

```markdown
## Step 3 — Start a node

On a GPU machine, with the `node` extra installed (see
[SETUP.md](SETUP.md)'s Node/GPU setup section) and the coordinator's cert
copied over, a node authenticates with its own self-generated keypair
plus a one-time GitHub sign-in (issue #39) — not the shared token from
Step 1.

**Get a GitHub token for the node's first registration.** Until issue #34
adds a built-in device-flow sign-in, obtain one by hand — e.g.
`gh auth token` if you have the GitHub CLI authenticated, or a Personal
Access Token from `github.com/settings/tokens` (classic or fine-grained;
no scopes are required, since only `GET /user` is called). Save it to a
file only the node can read:

```bash
echo "<your-github-token>" > ~/.mycelium/github-token
chmod 600 ~/.mycelium/github-token
```

This is only needed the **first** time this node's keypair registers —
the coordinator remembers the binding for as long as it keeps running
(see the limitation below), and a reconnecting node with an
already-known public key is accepted without it. It's harmless to keep
passing `--github-token-file` on every run regardless.

```bash
mycelium-node \
  --coordinator-url wss://<coordinator-ip>:8765 \
  --coordinator-cert ~/.mycelium/coordinator-cert.pem \
  --github-token-file ~/.mycelium/github-token
```
```

Then update item 3 of the "What happens" numbered list:

```markdown
3. Generates (on first run only — persisted afterward) an Ed25519
   keypair at `~/.mycelium/node-key.pem` by default, override with
   `--node-key-file`. Sends a registration message (model + node ID +
   public key + a signature proving it holds the matching private key,
   plus the GitHub token from `--github-token-file` if this is the
   key's first registration) and waits for the coordinator to ack it.
```

Add a new paragraph immediately after the existing "Each co-located node also needs its own `--node-key-file`..." paragraph (still inside Step 3, before the `SIGTERM`/`SIGHUP` paragraph):

```markdown
**A coordinator restart forgets every node's GitHub binding** (issue
#39) — bindings are in-memory only, exactly like the rest of the
coordinator's registry. Combined with GitHub OAuth's default 8-hour
access-token expiry, a volunteer's saved `--github-token-file` is quite
likely already expired by the time any coordinator restart happens, so a
restart can force a fresh GitHub sign-in, not just a free reconnect.
Whoever registers Mycelium's GitHub OAuth App (issue #34) should turn
*off* "Expire user access tokens" in that app's settings to avoid this
for real deployments.
```

- [ ] **Step 3: Verify the doc renders sensibly**

Run: `grep -n "token-file" docs/OPERATIONS.md`
Expected: no remaining node-registration references to `--token-file` (Step 2's coordinator `--token-file` and Step 5's client `--token-file` are correct and unchanged — only Step 1/Step 3's node-facing prose should have moved to `--github-token-file`/no token file at all).

- [ ] **Step 4: Mark the design doc implemented**

In `docs/superpowers/specs/2026-08-23-issue-39-github-oauth-identity-verification-design.md`, change line 4 from:

```
Status: Approved, not yet implemented
```

to:

```
Status: Implemented
```

- [ ] **Step 5: Commit**

```bash
git add docs/OPERATIONS.md docs/superpowers/specs/2026-08-23-issue-39-github-oauth-identity-verification-design.md
git commit -m "docs: document GitHub token node registration, mark issue #39 design implemented

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Final full-suite check**

Run: `.venv/bin/pytest -q`
Expected: PASS, zero failures
