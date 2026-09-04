# Exposure Handles and Per-Hop Timing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A completion reply tells the client which node and which bound identity served the hop — as opaque handles that cannot be resolved back to a public key or a GitHub login — plus how long the coordinator spent routing it.

**Architecture:** `crypto.py` gains a pure HMAC primitive alongside the `fingerprint()` it already has. `NodeRegistry` owns a per-process secret and composes the primitive with its own private identity map. `router.NodeDisconnectedError` splits into send-failed and dropped-while-waiting subclasses so the coordinator can tell whether a node actually received the request. `_handle_complete_request` accumulates an exposure list across the failover loop and times the whole loop.

**Tech Stack:** Python 3.11+, stdlib `hmac`/`hashlib`/`secrets`/`time`, `websockets` 17.0.1, `pytest` 9.1.1 with `pytest-asyncio` in `asyncio_mode = "auto"`.

## Global Constraints

- Issue: [#63](https://github.com/Zenkai-Dynamics/Mycelium/issues/63). Parent: [#54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54). Blocks #59.
- Design authority: `docs/superpowers/specs/2026-09-05-issue-63-exposure-handles-design.md`, and behind it `docs/superpowers/specs/2026-09-04-phase-2-implementation-design.md`. Where this plan and those disagree, the documents win.
- Branch: `phase-2/issue-63-exposure-handles`, off `main` (which already contains #55). One PR at the end.
- Handles are **16** hex characters — deliberately not the 12 that `crypto.fingerprint()` uses, so the two are never confused.
- The handle secret must NEVER be derived from the shared client token. Clients hold that token and could recompute every handle.
- `exposed` appears on every `complete_result` and `complete_error`, `[]` when nothing was sent. `elapsed_ms` appears only when at least one node was attempted.
- Existing `NodeDisconnectedError` behavior — `record_disconnect`, `unregister`, failover to another node — must be unchanged for both new subclasses.
- No test may make a real network call to GitHub or start a real vLLM. Fakes only.
- Style: match the surrounding code — long docstrings recording *why*, comments citing the issue number whose design doc explains a decision.
- Run the whole suite (`pytest`) before every commit. Every commit leaves it green. The suite takes ~110s and is currently at 269 passing.

---

### Task 1: The `handle` primitive

**Files:**
- Modify: `src/mycelium/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Produces: `crypto.HANDLE_LENGTH = 16`; `crypto.handle(secret: bytes, value: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_crypto.py`, matching the file's existing style:

```python
def test_handle_is_stable_for_the_same_secret_and_value():
    secret = b"a" * 32
    assert crypto.handle(secret, "some-public-key") == crypto.handle(secret, "some-public-key")


def test_handle_differs_for_different_values():
    secret = b"a" * 32
    assert crypto.handle(secret, "key-one") != crypto.handle(secret, "key-two")


def test_handle_differs_for_different_secrets():
    """The load-bearing property: a handle must depend on a secret the
    client does not have. A bare digest of the public key would pass the
    two tests above and still be trivially resolvable by anyone holding
    the value."""
    assert crypto.handle(b"a" * 32, "same-key") != crypto.handle(b"b" * 32, "same-key")


def test_handle_is_sixteen_hex_characters():
    value = crypto.handle(b"a" * 32, "some-public-key")
    assert len(value) == crypto.HANDLE_LENGTH == 16
    assert all(character in "0123456789abcdef" for character in value)


def test_handle_is_not_the_fingerprint_of_the_same_key():
    """Different lengths and different derivations — a handle must never
    be mistakable for the fingerprint the operator status view shows."""
    private_key = crypto.generate_keypair()
    public_key = crypto.public_key_b64(private_key)
    assert crypto.handle(b"a" * 32, public_key) != crypto.fingerprint(public_key)
```

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/test_crypto.py -k handle -v`
Expected: FAIL with `AttributeError: module 'mycelium.crypto' has no attribute 'handle'`.

- [ ] **Step 3: Implement**

Add `import hmac` to the imports in `src/mycelium/crypto.py`, then add below `fingerprint`:

```python
# Opaque-handle length, deliberately NOT equal to FINGERPRINT_LENGTH — a
# handle and a fingerprint have opposite disclosure properties (a
# fingerprint identifies a node to the operator; a handle exists so a
# client cannot identify anything), and different lengths make confusing
# one for the other visible. See the design doc for issue #63.
HANDLE_LENGTH = 16


def handle(secret: bytes, value: str) -> str:
    """Opaque, unresolvable identifier for `value`, derived under
    `secret` — used to tell a client which node and which identity served
    each hop of its flow without disclosing either.

    HMAC rather than a plain hash, specifically: clients hold the shared
    token and know the model strings they asked for, so any digest they
    could recompute from data they already have would be resolvable by
    them. The secret is what makes a handle groupable but not
    identifying. See the design doc for issue #63 and ADR-0004.
    """
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()[:HANDLE_LENGTH]
```

- [ ] **Step 4: Run and confirm they pass**

Run: `pytest tests/test_crypto.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/crypto.py tests/test_crypto.py
git commit -m "feat: opaque handle primitive for exposure reporting (#63)

HMAC-SHA256 truncated to 16 hex characters — deliberately a different
length from the 12-character fingerprint, so the two are never confused.
HMAC rather than a bare digest because a client holding the shared token
could otherwise recompute and resolve any hash of data it already has.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The registry owns the secret

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Test: `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: `crypto.handle` from Task 1.
- Produces: `NodeRegistry(token, identity_verifier=None, per_identity_cap=3, random_source=None, handle_secret: bytes | None = None)`; `registry.handles_for(public_key: str) -> dict` returning `{"node_handle": str, "identity_handle": str | None}`.

- [ ] **Step 1: Write the failing tests**

Read `tests/coordinator/test_registry.py` first and reuse whatever helpers it already has for building a registry and binding an identity — that file has established patterns for `resolve_identity` with a fake verifier. Add:

```python
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
```

If `_fake_verifier`, `PUBKEY_A` or `PUBKEY_B` are named differently in that file, use its names. If it has no fake verifier, copy the one-line shape used in `tests/coordinator/test_server.py`'s `_fake_identity_verifier` rather than inventing a new mechanism.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/coordinator/test_registry.py -k handle -v`
Expected: FAIL — `NodeRegistry.__init__` has no `handle_secret` parameter.

- [ ] **Step 3: Implement**

Add `import secrets` to `src/mycelium/coordinator/registry.py`. Extend `__init__`'s signature with `handle_secret: bytes | None = None` as the last parameter, and set it alongside the other injected collaborators:

```python
        # Per-process and in-memory, exactly like the identity bindings,
        # bans and reputation counters above — so handles are not
        # comparable across a coordinator restart. That costs nothing: a
        # flow lives in one client process. Persisting it would create
        # the first on-disk artifact capable of linking one client's
        # flows across time. Injectable for tests only (see
        # identity_verifier and random_source for the same pattern);
        # production callers take the random default. Deliberately NOT
        # derived from the shared token — clients hold that, and could
        # then resolve every handle. See the design doc for issue #63.
        self._handle_secret = handle_secret or secrets.token_bytes(32)
```

Then add the method, next to `list_nodes`:

```python
    def handles_for(self, public_key: str) -> dict:
        """Opaque per-coordinator-process handles for the node at
        `public_key` and for the GitHub identity bound to it — what a
        client is told about who served its hop.

        A client can group hops by these ("did one volunteer read three
        of my five hops?") but cannot resolve them to a public key,
        fingerprint or GitHub login. That distinction is the whole point:
        ADR-0004's exposure claim has to be checkable by the client
        without handing clients the volunteer identities issue #56
        deliberately withholds.

        identity_handle is None if public_key has no bound identity.
        Registration always binds one, so that shouldn't happen — but a
        completion must not fail because a reporting field couldn't be
        built, and None here means "unexpectedly unbound", never
        "anonymous node".
        """
        identity = self._identity_by_key.get(public_key)
        return {
            "node_handle": crypto.handle(self._handle_secret, public_key),
            "identity_handle": (
                crypto.handle(self._handle_secret, identity.id) if identity is not None else None
            ),
        }
```

- [ ] **Step 4: Run and confirm they pass**

Run: `pytest tests/coordinator/test_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and commit**

Run: `pytest`
Expected: PASS — this task is purely additive; nothing calls `handles_for` yet.

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: NodeRegistry derives per-process exposure handles (#63)

The secret is generated at construction and never persisted, matching
identity bindings, bans and reputation counters. Injectable so tests can
assert exact handle values; production callers take the random default.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Split the disconnect error

**Files:**
- Modify: `src/mycelium/coordinator/router.py`
- Modify: `src/mycelium/coordinator/server.py` (the `_handle_registration` cleanup only)
- Test: `tests/coordinator/test_router.py`

**Interfaces:**
- Produces: `router.NodeSendFailedError(NodeDisconnectedError)` — raised when the send itself failed, meaning nothing reached the node; `router.NodeDroppedError(NodeDisconnectedError)` — raised when the connection died while awaiting a reply, meaning the node very likely received the request.

Both subclass the existing `NodeDisconnectedError`, so every current `except router.NodeDisconnectedError` site — including the coordinator's failover branch — keeps working untouched. Behavior does not change in this task; only the ability to tell the two cases apart.

- [ ] **Step 1: Write the failing tests**

`tests/coordinator/test_router.py` already has `_make_node()`, `_FakeNodeWebsocket(send_raises=...)` and `_resolve_after`. Use them.

```python
async def test_send_failure_raises_node_send_failed():
    node = _make_node(_FakeNodeWebsocket(send_raises=ConnectionClosedError(None, None)))

    with pytest.raises(router.NodeSendFailedError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_send_failure_is_still_a_node_disconnected_error():
    """Both new classes subclass NodeDisconnectedError so the
    coordinator's existing failover branch is untouched."""
    node = _make_node(_FakeNodeWebsocket(send_raises=ConnectionClosedError(None, None)))

    with pytest.raises(router.NodeDisconnectedError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_drop_while_awaiting_reply_raises_node_dropped():
    """The distinction that matters for exposure: this node received the
    request, so it must be reported as having seen the content."""
    node = _make_node()

    async def drop_after(delay):
        await asyncio.sleep(delay)
        (request_id,) = node.pending.keys()
        node.pending[request_id].set_exception(router.NodeDroppedError("node dropped"))

    asyncio.create_task(drop_after(0.05))

    with pytest.raises(router.NodeDroppedError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)
```

Check the existing file for how it constructs `ConnectionClosedError` — reuse that exact construction rather than the placeholder above if it differs.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/coordinator/test_router.py -v`
Expected: FAIL — `module 'mycelium.coordinator.router' has no attribute 'NodeSendFailedError'`.

- [ ] **Step 3: Add the subclasses**

In `src/mycelium/coordinator/router.py`, immediately after the existing `NodeDisconnectedError` class, add:

```python
class NodeSendFailedError(NodeDisconnectedError):
    """The connection was already unusable when we tried to send — so
    nothing reached the node, and it saw none of the request's content.

    Split out of NodeDisconnectedError for issue #63: this module's
    docstring used to say a caller "shouldn't need to (and can't)"
    distinguish a failed send from a mid-flight drop, which was true
    until exposure reporting existed. It is a subclass, so callers that
    only care about "this node is gone, try another" are unaffected.
    """


class NodeDroppedError(NodeDisconnectedError):
    """The connection died while we were awaiting a reply — so the node
    very likely received and began processing the request, and must be
    reported as having seen its content. See the design doc for issue
    #63."""
```

Update `NodeDisconnectedError`'s own docstring so it no longer claims the two cases can't be distinguished, and note that it remains the class to catch when you only care that the node is unusable.

- [ ] **Step 4: Raise the specific classes**

In `route_request`, the send's `except` clause becomes:

```python
        except websockets.exceptions.ConnectionClosed as exc:
            raise NodeSendFailedError(f"node {node.node_id!r} disconnected: {exc}") from exc
```

In `src/mycelium/coordinator/server.py`, `_handle_registration`'s `finally` block sets an exception on every still-pending future when a connection dies mid-request — which is exactly the "node probably read it" case:

```python
                pending_future.set_exception(
                    router.NodeDroppedError(f"node {node_id!r} disconnected mid-request")
                )
```

- [ ] **Step 5: Run the whole suite and commit**

Run: `pytest`
Expected: PASS, unchanged count. Every existing test catching `NodeDisconnectedError` still catches both subclasses.

```bash
git add src/mycelium/coordinator/router.py src/mycelium/coordinator/server.py \
        tests/coordinator/test_router.py
git commit -m "refactor: distinguish a failed send from a mid-flight drop (#63)

NodeSendFailedError and NodeDroppedError, both subclassing
NodeDisconnectedError so every existing catch site and the failover path
are untouched. The distinction is what lets the coordinator report
whether a node actually received a request's content.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Report exposure and timing on the wire

**Files:**
- Modify: `src/mycelium/coordinator/server.py` (`_handle_complete_request` and `serve`)
- Test: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `registry.handles_for` (Task 2), `router.NodeSendFailedError` / `NodeDroppedError` (Task 3).
- Produces: `complete_result` gains `exposed: list[dict]` and `elapsed_ms: int`; `complete_error` gains `exposed`, and `elapsed_ms` when at least one node was attempted. `serve(...)` gains a `handle_secret: bytes | None = None` passthrough.

- [ ] **Step 1: Loosen the six exact-equality assertions**

These currently assert the whole reply dict and will break the moment fields are added. They are at `tests/coordinator/test_server.py` lines 801, 1011, 1289, 1385, 1386 and 1464. Change each from whole-dict equality to asserting the fields it actually cares about, e.g.:

```python
                assert response["type"] == "complete_result"
                assert response["text"] == "echo: hello"
```

Do not weaken anything else about them. Run `pytest tests/coordinator/test_server.py -v` after this step and confirm still-green before adding behavior — this is a pure test refactor and must not change any outcome.

- [ ] **Step 2: Write the failing tests**

Copy the fixture shape from the existing `test_complete_request_with_messages_reaches_the_node_intact`. Inject a fixed secret through `server.serve(..., handle_secret=b"s" * 32)` so handles are assertable.

```python
async def test_complete_result_carries_exposure_handles_and_timing(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_result", "text": "ok"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())

            node_task.cancel()

    assert len(response["exposed"]) == 1
    assert response["exposed"][0]["node_handle"] == crypto.handle(b"s" * 32, public_key)
    assert response["exposed"][0]["identity_handle"]
    assert isinstance(response["elapsed_ms"], int)
    # The reply must not carry anything that resolves a volunteer.
    assert "public_key" not in response
    assert "fingerprint" not in response
    assert "identity" not in response
    assert public_key not in json.dumps(response)
    assert crypto.fingerprint(public_key) not in json.dumps(response)


async def test_no_healthy_node_reports_empty_exposure_and_no_timing(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "nope", "prompt": "hi"}
            ))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert response["exposed"] == []
    assert "elapsed_ms" not in response, (
        "nothing was routed, so reporting a duration would assert routing took no time"
    )


async def test_malformed_request_reports_empty_exposure(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps({
                "type": "complete", "token": "secret-token", "model": "m",
                "messages": [{"role": "user"}],
            }))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert response["exposed"] == []


async def test_node_reported_failure_still_reports_that_node_as_exposed(tmp_path):
    """A node that read the request and then failed still read it."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
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
                response = json.loads(await client_ws.recv())

            node_task.cancel()

    assert response["type"] == "complete_error"
    assert response["exposed"][0]["node_handle"] == crypto.handle(b"s" * 32, public_key)
    assert isinstance(response["elapsed_ms"], int)


async def test_failover_reports_both_the_dropped_node_and_the_one_that_answered(
    tmp_path, monkeypatch
):
    """The case a single-handle report would silently under-count: node A
    received the request and then dropped, node B answered. Both read it.

    The mid-flight-drop shape (receive the routed request, then close
    without replying) is the same one
    test_complete_request_disconnect_increments_disconnect_counter uses.
    Node A is picked first deterministically: with no reputation history
    every candidate weighs the same, so find_node_for_model falls back to
    registration order.
    """
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        node_a_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload_a, public_key_a = _register_payload("node-a", "m")
        await node_a_ws.send(json.dumps(payload_a))
        await node_a_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b_ws:
            payload_b, public_key_b = _register_payload("node-b", "m")
            await node_b_ws.send(json.dumps(payload_b))
            await node_b_ws.recv()

            async def a_receives_then_dies():
                await node_a_ws.recv()
                await node_a_ws.close()

            async def b_answers():
                routed = json.loads(await node_b_ws.recv())
                await node_b_ws.send(json.dumps({
                    "type": "complete_result",
                    "request_id": routed["request_id"],
                    "text": "ok",
                }))

            a_task = asyncio.create_task(a_receives_then_dies())
            b_task = asyncio.create_task(b_answers())

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())

            await a_task
            await b_task

    assert response["type"] == "complete_result"
    assert [entry["node_handle"] for entry in response["exposed"]] == [
        crypto.handle(b"s" * 32, public_key_a),
        crypto.handle(b"s" * 32, public_key_b),
    ], "the node that received-then-dropped read the request too, and must be reported"


async def test_a_node_whose_send_failed_is_not_reported_as_exposed(tmp_path, monkeypatch):
    """NodeSendFailedError means the bytes never left the coordinator, so
    that node saw nothing and must not appear in the report.

    route_request is faked rather than provoked: a real send-failure
    needs a node whose socket is dead but which is still registered, and
    the server's own disconnect cleanup races to unregister it — so the
    genuine article cannot be staged deterministically end to end. The
    router-level tests in Task 3 cover raising it for real; this covers
    the coordinator's handling of it.
    """
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    routed_to: list[str] = []

    async def send_fails_on_first_node(node, messages, timeout=None):
        routed_to.append(node.public_key)
        if len(routed_to) == 1:
            raise router.NodeSendFailedError(f"node {node.node_id!r} disconnected")
        return "ok"

    monkeypatch.setattr(router, "route_request", send_fails_on_first_node)

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_a_ws:
            payload_a, public_key_a = _register_payload("node-a", "m")
            await node_a_ws.send(json.dumps(payload_a))
            await node_a_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b_ws:
                payload_b, public_key_b = _register_payload("node-b", "m")
                await node_b_ws.send(json.dumps(payload_b))
                await node_b_ws.recv()

                async with websockets.connect(
                    f"wss://127.0.0.1:{port}", ssl=client_ctx
                ) as client_ws:
                    await client_ws.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                    ))
                    response = json.loads(await client_ws.recv())

    assert routed_to == [public_key_a, public_key_b]
    assert [entry["node_handle"] for entry in response["exposed"]] == [
        crypto.handle(b"s" * 32, public_key_b)
    ], "node A's send never landed, so it read nothing and must not be reported"
```

- [ ] **Step 3: Run and confirm they fail**

Run: `pytest tests/coordinator/test_server.py -k "exposure or exposed or failover" -v`
Expected: FAIL — `serve()` has no `handle_secret` parameter, and replies carry no `exposed` key.

- [ ] **Step 4: Thread the secret through `serve`**

In `src/mycelium/coordinator/server.py`, add `handle_secret: bytes | None = None` as the last parameter of `serve(...)`, document it in the docstring alongside the existing notes on `identity_verifier` and `per_identity_cap` (same rationale: injected by tests, never by production callers), and pass it to the `NodeRegistry(...)` construction.

- [ ] **Step 5: Accumulate exposure and time the loop**

Add `import time` to the module imports. Rewrite `_handle_complete_request`'s body from the `reject` helper down to the final send:

```python
    exposed: list[dict] = []
    attempts = 0
    started = time.monotonic()

    async def reject(reason: str) -> None:
        reply = {"type": "complete_error", "reason": reason, "exposed": exposed}
        if attempts:
            reply["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        try:
            await websocket.send(json.dumps(reply))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.close()
```

Keep the `model` guard and the `normalize_messages` call exactly as they are — they run before any attempt, so `exposed` is `[]` and `attempts` is 0, and `reject` produces the right shape without special-casing.

In the loop, increment `attempts` immediately after a node is picked, and append the node's handles in every branch except `NodeSendFailedError`:

```python
    tried: set[str] = set()
    while True:
        try:
            node = registry.find_node_for_model(model, exclude=frozenset(tried))
            if node is None:
                raise router.NoHealthyNodeError(f"no healthy node for model {model!r}")
            attempts += 1
            text = await router.route_request(
                node, messages, timeout=router.NODE_COMPLETE_TIMEOUT_SECONDS
            )
        except router.NodeSendFailedError:
            # Nothing reached this node — it saw none of the content, so
            # it is deliberately NOT added to `exposed`. See the design
            # doc for issue #63.
            registry.record_disconnect(node.public_key)
            registry.unregister(node.public_key, node.websocket)
            tried.add(node.public_key)
            continue
        except router.NodeDisconnectedError:
            # NodeDroppedError: the node received the request and then
            # died, so it very likely read the content. Reported as
            # exposure even though it never answered.
            exposed.append(registry.handles_for(node.public_key))
            registry.record_disconnect(node.public_key)
            registry.unregister(node.public_key, node.websocket)
            tried.add(node.public_key)
            continue
        except router.NodeTimeoutError as exc:
            exposed.append(registry.handles_for(node.public_key))
            registry.record_timeout(node.public_key)
            await reject(str(exc))
            return
        except router.NodeError as exc:
            exposed.append(registry.handles_for(node.public_key))
            registry.record_crash(node.public_key)
            await reject(str(exc))
            return
        except router.RoutingError as exc:
            await reject(str(exc))
            return
        else:
            exposed.append(registry.handles_for(node.public_key))
            registry.record_completion(node.public_key)
            break

    reply = {
        "type": "complete_result",
        "text": text,
        "exposed": exposed,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    try:
        await websocket.send(json.dumps(reply))
    except websockets.exceptions.ConnectionClosed:
        return
    await websocket.close()
```

Note the ordering constraint: `except router.NodeSendFailedError` **must** come before `except router.NodeDisconnectedError`, since the former is a subclass of the latter and Python takes the first matching branch.

Extend the function's docstring to record that it now reports which nodes received the request and how long routing took, citing issue #63.

- [ ] **Step 6: Run the whole suite**

Run: `pytest`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: report per-hop exposure handles and routing time (#63)

Every reply carries `exposed` — the handles of nodes that actually
received the request, a list because failover can expose more than one
node for a single hop. A node that received the request and then dropped,
timed out, or failed is reported: it read the content either way. A node
whose send never landed is not.

elapsed_ms spans the whole retry loop, so failover time is not silently
reattributed to client-coordinator overhead when #61 measures it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Glossary

**Files:**
- Modify: `CONTEXT.md`

- [ ] **Step 1: Add the term**

`CONTEXT.md`'s "Phase 2 — multi-model flows" section already defines **Exposure**. Add **Handle** immediately after it, matching the file's format exactly — bold term, colon, definition, and an `_Avoid_:` line where the file uses one:

```markdown
**Handle**:
An opaque identifier for a node or a bound identity, returned with each
completion so a client can tell which hops one volunteer served. Derived
under a secret the coordinator generates at startup, so a client can group
by it but never resolve it to a public key, fingerprint or GitHub login.
Not stable across a coordinator restart, and not the same thing as a
fingerprint — which identifies a node *to the operator*.
_Avoid_: id, fingerprint, token
```

- [ ] **Step 2: Verify it reads correctly in context**

Run: `grep -n -A6 "^\*\*Exposure\*\*" CONTEXT.md`
Confirm the new entry sits alongside the existing Phase 2 terms and matches their formatting.

- [ ] **Step 3: Commit and open the PR**

```bash
git add CONTEXT.md
git commit -m "docs: define 'handle' in the glossary (#63)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin phase-2/issue-63-exposure-handles
```

Open the PR against `main`, titled `Exposure handles and per-hop timing on the wire (#63)`. The body should state: what a handle is and why it is an HMAC rather than a digest; that failed hops and failover both report exposure, with the reasoning; that `elapsed_ms` spans the whole loop and why that matters to #61; and the honest caveat that handles are stable for a coordinator's lifetime, so this is "we do not hand volunteer identities to clients", not unlinkability.

---

## Acceptance criteria mapping

| #63 criterion | Where it is met |
|---|---|
| A reply carries a node handle and an identity handle | Task 4 Step 2 |
| Same node → same handle; two nodes under one identity → same identity handle | Task 2 Step 1 |
| Handles not resolvable to key, fingerprint or login by a client | Task 1 Step 1, Task 2 Step 1, Task 4 Step 2 (reply-shape assertions) |
| Handles differ across coordinator restarts | Task 2 Step 1 (`test_two_registries_without_an_injected_secret_differ`) |
| A hop that failed at the node still reports that node's handles | Task 4 Step 2 (`..._node_reported_failure_still_reports...`) |
| A failure where nothing reached a node reports no handles | Task 4 Step 2 (no-healthy-node, malformed, and the send-failure case) |
| Sent to one node, dropped, succeeded on another → both reported | Task 4 Step 2 (failover test) |
| `elapsed_ms` measures the coordinator's routing time | Task 4 Steps 2 and 5 |
| `CONTEXT.md` gains **handle** | Task 5 |
| Existing `NodeDisconnectedError` behavior unchanged | Task 3 Step 5 — whole suite green with no test changes to the failover path |

## Notes for whoever picks this up

- Do **not** add a `fault` field or `ClientRequestError`. That is #58, which lands next on top of this and will restructure the same retry loop again. Leaving it out keeps the two diffs reviewable.
- The client library ignores these fields for now. `Flow` consumes them in #59.
- If a test needs the mid-flight-drop case and the existing failover helpers only cover send-failure, write the mid-flight variant rather than stretching an existing helper to do both — the whole point of Task 3 is that those two cases are different.
