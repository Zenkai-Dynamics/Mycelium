# Client-Facing Model Discovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A client can ask the coordinator which models are currently served, and get back model strings with healthy-node counts — and nothing that identifies a volunteer.

**Architecture:** A new `list_models` branch in the coordinator's first-message dispatch, backed by a new `NodeRegistry.list_models()` built up from nothing rather than filtered down from the operator view. Two client surfaces consume it: a `mycelium-client-models` entry point and `Flow.list_models()`, both through the shared `client/transport.py`.

**Tech Stack:** Python 3.11+, `websockets` 17.0.1, stdlib `argparse`/`asyncio`, `pytest` 9.1.1 with `pytest-asyncio` in `asyncio_mode = "auto"`.

## Global Constraints

- Issue: [#56](https://github.com/Zenkai-Dynamics/Mycelium/issues/56). Parent: [#54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54).
- Design authority: `docs/superpowers/specs/2026-09-06-issue-56-model-discovery-design.md`. Where this plan and that document disagree, the document wins.
- Branch: `phase-2/issue-56-model-discovery`, off `main`. One PR at the end.
- **The reply must never carry a fingerprint, a GitHub identity, or a reputation counter.** Build the response up from model strings and counts; do not derive it by stripping fields from `list_nodes()`.
- Models are sorted by model string. An empty registry returns an empty list, not an error.
- "Healthy" means registered — the keepalive already evicts the dead.
- Gated by the same shared client token as `complete`.
- Both client surfaces go through `client/transport.py`. Do not hand-roll another round trip.
- Do **not** migrate `status_cli` or `ban_cli` onto the transport — out of scope.
- No test may make a real network call to GitHub or start a real vLLM. Fakes only.
- Style: match the surrounding code — long docstrings recording *why*, comments citing the issue number whose design doc explains a decision.
- Run the whole suite (`pytest`) before every commit. Every commit leaves it green. The suite is currently at 346 passing.

---

### Task 1: `NodeRegistry.list_models()`

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Test: `tests/coordinator/test_registry.py`

**Interfaces:**
- Produces: `NodeRegistry.list_models() -> list[dict]`, each `{"model": str, "healthy_nodes": int}`, sorted by model string.

- [ ] **Step 1: Write the failing tests**

`tests/coordinator/test_registry.py` already has `PUBKEY_A`, `PUBKEY_B` and builds registries directly. Follow its existing style; add a third public key locally if the file has only two.

```python
def test_list_models_is_empty_when_nothing_is_registered():
    assert NodeRegistry("token").list_models() == []


def test_list_models_counts_nodes_per_model():
    registry = NodeRegistry("token")
    registry.register(PUBKEY_A, "node-a", "model-one", object())
    registry.register(PUBKEY_B, "node-b", "model-one", object())

    assert registry.list_models() == [{"model": "model-one", "healthy_nodes": 2}]


def test_list_models_lists_distinct_models_sorted():
    """Sorted so the response is deterministic for a given registry state —
    registry iteration is insertion order, which would otherwise shuffle as
    volunteers come and go. See the design doc for issue #56."""
    registry = NodeRegistry("token")
    registry.register(PUBKEY_A, "node-a", "zeta-model", object())
    registry.register(PUBKEY_B, "node-b", "alpha-model", object())

    assert registry.list_models() == [
        {"model": "alpha-model", "healthy_nodes": 1},
        {"model": "zeta-model", "healthy_nodes": 1},
    ]


def test_list_models_drops_a_model_whose_node_unregistered():
    registry = NodeRegistry("token")
    socket_a = object()
    registry.register(PUBKEY_A, "node-a", "model-one", socket_a)
    registry.register(PUBKEY_B, "node-b", "model-two", object())

    registry.unregister(PUBKEY_A, socket_a)

    assert registry.list_models() == [{"model": "model-two", "healthy_nodes": 1}]


def test_list_models_exposes_nothing_identifying():
    """The whole point of a separate method: no fingerprint, identity or
    reputation may reach a client. See the design doc for issue #56."""
    registry = NodeRegistry("token")
    registry.register(PUBKEY_A, "node-a", "model-one", object())
    registry.record_completion(PUBKEY_A)

    (entry,) = registry.list_models()

    assert set(entry) == {"model", "healthy_nodes"}
```

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/coordinator/test_registry.py -k list_models -v`
Expected: FAIL — `NodeRegistry` has no attribute `list_models`.

- [ ] **Step 3: Implement**

In `src/mycelium/coordinator/registry.py`, add beside `list_nodes`:

```python
    def list_models(self) -> list[dict]:
        """The distinct models currently served, with how many registered
        nodes serve each — the client-facing view of the registry.

        Deliberately built up from model strings and counts rather than
        derived by stripping fields from list_nodes(): a client token must
        not be able to enumerate volunteer identities, and a subtractive
        view would expose every future operator-facing field by default
        until someone remembered to strip it. See the design doc for
        issue #56.

        Sorted by model string so the response is deterministic for a
        given registry state — registry iteration is insertion order,
        which would otherwise shuffle as volunteers come and go.

        "Healthy" means registered: a node that stops answering the
        keepalive is already evicted (see the design doc for issue #9),
        so registration is liveness. Advisory only — a node can
        disconnect between a client's check and its call.
        """
        counts: dict[str, int] = {}
        for node in self._nodes.values():
            counts[node.model] = counts.get(node.model, 0) + 1
        return [
            {"model": model, "healthy_nodes": counts[model]}
            for model in sorted(counts)
        ]
```

- [ ] **Step 4: Run and confirm they pass, then the whole suite**

Run: `pytest tests/coordinator/test_registry.py -v` then `pytest`
Expected: PASS. Purely additive — nothing calls it yet.

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: NodeRegistry.list_models, the client-facing registry view (#56)

Built up from model strings and counts rather than filtered down from
list_nodes(): a subtractive view would expose every future operator field
by default until someone remembered to strip it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The `list_models` request on the wire

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Test: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `registry.list_models()` from Task 1.
- Produces: client→coordinator `{"type": "list_models", "token": ...}` → `{"type": "models", "models": [...]}`.

- [ ] **Step 1: Write the failing tests**

Model these on `test_complete_request_with_no_matching_node_returns_error` and the existing status-query tests — they show the fixture shape for standing up a coordinator and registering fake nodes.

```python
async def test_list_models_reports_each_served_model_with_its_count(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_a:
            payload_a, _ = _register_payload("node-a", "model-one")
            await node_a.send(json.dumps(payload_a))
            await node_a.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b:
                payload_b, _ = _register_payload("node-b", "model-two")
                await node_b.send(json.dumps(payload_b))
                await node_b.recv()

                async with websockets.connect(
                    f"wss://127.0.0.1:{port}", ssl=client_ctx
                ) as client_ws:
                    await client_ws.send(json.dumps(
                        {"type": "list_models", "token": "secret-token"}
                    ))
                    response = json.loads(await client_ws.recv())

    assert response["type"] == "models"
    assert response["models"] == [
        {"model": "model-one", "healthy_nodes": 1},
        {"model": "model-two", "healthy_nodes": 1},
    ]


async def test_list_models_is_empty_with_no_nodes(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps({"type": "list_models", "token": "secret-token"}))
            response = json.loads(await client_ws.recv())

    assert response == {"type": "models", "models": []}


async def test_list_models_exposes_nothing_that_identifies_a_volunteer(tmp_path):
    """An allowlist, not a denylist: a field nobody thought to forbid is how
    a fingerprint or a GitHub login reaches a client-facing wire unnoticed.
    See the design doc for issue #56."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "model-one")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()

            async with websockets.connect(
                f"wss://127.0.0.1:{port}", ssl=client_ctx
            ) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "list_models", "token": "secret-token"}
                ))
                response = json.loads(await client_ws.recv())

    assert set(response) == {"type", "models"}
    assert set(response["models"][0]) == {"model", "healthy_nodes"}
    serialised = json.dumps(response)
    assert public_key not in serialised
    assert crypto.fingerprint(public_key) not in serialised
    assert "octocat" not in serialised


async def test_list_models_with_a_wrong_token_is_closed_without_reply(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps({"type": "list_models", "token": "wrong"}))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await client_ws.recv()
```

The identity assertion uses `"octocat"` because that is the login `_fake_identity_verifier` returns in this file — confirm that before relying on it.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/coordinator/test_server.py -k list_models -v`
Expected: FAIL — an unrecognized message type closes the connection, so the tests expecting a reply hang up or error rather than seeing `models`.

- [ ] **Step 3: Implement**

In `_handle_node`'s dispatch chain in `src/mycelium/coordinator/server.py`, add a branch beside the others:

```python
    if message_type == "list_models":
        await _handle_list_models(websocket, registry, message)
        return
```

and the handler, next to `_handle_status_query`:

```python
async def _handle_list_models(websocket, registry: NodeRegistry, message: dict) -> None:
    """A client's model-discovery request: authenticate, return the models
    currently served with their healthy-node counts, close.

    Gated by the same shared client token as a completion, and deliberately
    returns strictly less than the operator status view — no fingerprints,
    no bound identities, no reputation counters. A client token must not be
    able to enumerate volunteer identities. See the design doc for issue
    #56.

    Advisory only: a node can disconnect between this reply and the
    client's next call, so this can never be a guarantee. A model that
    vanishes in between fails that hop and surfaces to the caller
    unchanged.
    """
    if not registry.check_token(message.get("token")):
        await websocket.close()
        return
    await websocket.send(json.dumps({"type": "models", "models": registry.list_models()}))
    await websocket.close()
```

- [ ] **Step 4: Run and confirm they pass, then the whole suite**

Run: `pytest tests/coordinator/test_server.py -v` then `pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: list_models request on the client-facing wire (#56)

Gated by the same shared client token as a completion, and returning
strictly less than the operator status view. The reply's key set is
asserted exactly rather than checked for absent fields — a field nobody
thought to forbid is how a fingerprint reaches a client unnoticed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `mycelium-client-models`

**Files:**
- Create: `src/mycelium/client/models_cli.py`
- Modify: `pyproject.toml`
- Create: `tests/client/test_models_cli.py`

**Interfaces:**
- Consumes: `transport.request`, `transport.TransportError`.
- Produces: `models_cli.parse_args(argv)`, `models_cli.list_models(...) -> list[dict]`, `models_cli.main() -> None`, and the `mycelium-client-models` console script.

- [ ] **Step 1: Write the failing tests**

`tests/client/test_transport.py` shows the shape for standing up a real coordinator; reuse it rather than inventing a fixture.

```python
"""Tests for mycelium.client.models_cli."""

import json
import ssl

import pytest
import websockets

from mycelium import crypto
from mycelium.client import models_cli
from mycelium.coordinator import certs, github_identity, server


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")


def test_parse_args_requires_the_connection_flags():
    with pytest.raises(SystemExit):
        models_cli.parse_args([])


async def test_list_models_returns_what_the_coordinator_serves(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            private_key = crypto.generate_keypair()
            await node_ws.send(json.dumps({
                "type": "register", "model": "model-one", "node_id": "node-a",
                "public_key": crypto.public_key_b64(private_key),
                "signature": crypto.sign_public_key(private_key),
                "github_token": "valid-github-token",
            }))
            await node_ws.recv()

            models = await models_cli.list_models(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token"
            )

    assert models == [{"model": "model-one", "healthy_nodes": 1}]


async def test_list_models_raises_on_a_rejected_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        with pytest.raises(models_cli.QueryError):
            await models_cli.list_models(
                f"wss://127.0.0.1:{port}", cert_path, "wrong-token"
            )
```

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/client/test_models_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'models_cli'`.

- [ ] **Step 3: Write the module**

Create `src/mycelium/client/models_cli.py`, modelled on `coordinator/status_cli.py`'s shape but going through the shared transport:

```python
"""CLI entry point for asking the coordinator which models are served.

A separate entry point rather than a subcommand of mycelium-client,
matching the mycelium-coordinator-status / -ban precedent. See the design
doc for issue #56.

Unlike the operator CLIs, this one goes through mycelium.client.transport
— the shared round trip extracted in issue #59 — rather than hand-rolling
connect-send-receive against a wire three issues have already changed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mycelium.client import transport

LIST_MODELS_TIMEOUT_SECONDS = 10.0


class QueryError(Exception):
    """Raised when the discovery request fails: the coordinator rejected it
    (a wrong token closes the connection without replying), it could not be
    reached, or it answered with something unexpected."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-client-models")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    return parser.parse_args(argv)


async def list_models(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    timeout: float = LIST_MODELS_TIMEOUT_SECONDS,
) -> list[dict]:
    """Ask the coordinator which models are currently served.

    Advisory only: a node can disconnect between this answer and a later
    completion, so the result is a snapshot, never a guarantee.
    """
    try:
        reply = await transport.request(
            coordinator_url, coordinator_cert,
            {"type": "list_models", "token": token}, timeout,
        )
    except transport.TransportError as exc:
        raise QueryError(str(exc)) from None
    if reply.get("type") != "models":
        raise QueryError(f"unexpected response from coordinator: {reply!r}")
    return reply["models"]


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    try:
        models = asyncio.run(
            list_models(args.coordinator_url, args.coordinator_cert, token)
        )
    except QueryError as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    if not models:
        print("No models currently served.")
        return
    for entry in models:
        nodes = entry["healthy_nodes"]
        print(f"{entry['model']}  {nodes} node{'' if nodes == 1 else 's'}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Register the entry point**

In `pyproject.toml`, under `[project.scripts]`, beside the existing five:

```toml
mycelium-client-models = "mycelium.client.models_cli:main"
```

Then reinstall so the script exists: `pip install -e .` (or `uv pip install -e .`, matching however the worktree venv was created).

- [ ] **Step 5: Run the tests, verify the script, then the whole suite**

Run: `pytest tests/client/test_models_cli.py -v`
Then: `python -c "from mycelium.client.models_cli import parse_args; print(parse_args(['--coordinator-url','wss://x','--coordinator-cert','c.pem','--token-file','t.txt']))"`
Expected: a Namespace with all three fields. Then `pytest` for the full suite.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/client/models_cli.py pyproject.toml tests/client/test_models_cli.py
git commit -m "feat: mycelium-client-models entry point (#56)

Flat argparse matching the operator CLIs' precedent, but going through
the shared transport rather than hand-rolling a fourth copy of
connect-send-receive.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `Flow.list_models()`

**Files:**
- Modify: `src/mycelium/client/flow.py`
- Modify: `src/mycelium/client/__init__.py`
- Test: `tests/client/test_flow.py`

**Interfaces:**
- Produces: `await Flow.list_models(timeout=...) -> list[dict]`; `TransportError` added to `mycelium.client`'s exports.

This is the half of a decision #59 deferred: it dropped the method because the wire request did not exist, with the explicit intent of adding it when #56 landed.

- [ ] **Step 1: Write the failing tests**

`tests/client/test_flow.py` already has `_FakeCoordinator`, `_queued`, `_serve_fake` and imports. Reuse them.

```python
async def test_list_models_returns_what_the_coordinator_reports(tmp_path):
    fake = _FakeCoordinator(_queued([{
        "type": "models",
        "models": [
            {"model": "small-model", "healthy_nodes": 1},
            {"model": "big-model", "healthy_nodes": 2},
        ],
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        models = await flow.list_models()
    finally:
        running.close()
        await running.wait_closed()

    assert models == [
        {"model": "small-model", "healthy_nodes": 1},
        {"model": "big-model", "healthy_nodes": 2},
    ]
    assert fake.received[0] == {"type": "list_models", "token": "secret-token"}


async def test_list_models_does_not_touch_the_flow_record(tmp_path):
    """Discovery is not a hop: nothing was sent to a node, nobody saw
    anything, and there is no Hop to record. See the design doc for #56."""
    fake = _FakeCoordinator(_queued([{"type": "models", "models": []}]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await flow.list_models()
    finally:
        running.close()
        await running.wait_closed()

    assert flow.hops == []
    assert flow.exposure().by_node == {}


async def test_list_models_propagates_a_transport_failure(tmp_path):
    """A discovery failure is not a hop failure, so HopError would be the
    wrong type — there is no Hop to attach and no fault to report."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    flow = Flow("wss://127.0.0.1:1", cert_path, "secret-token")
    with pytest.raises(transport.TransportError):
        await flow.list_models()


def test_transport_error_is_exported_from_the_package():
    from mycelium.client import TransportError

    assert TransportError is transport.TransportError
```

Add `from mycelium.client import transport` to the test file's imports if it is not already there.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/client/test_flow.py -k list_models -v`
Expected: FAIL — `Flow` has no attribute `list_models`.

- [ ] **Step 3: Implement**

Add the method to `Flow` in `src/mycelium/client/flow.py`, after `call`:

```python
    async def list_models(self, timeout: float = CALL_TIMEOUT_SECONDS) -> list[dict]:
        """Ask the coordinator which models are currently served.

        Returns `[{"model": str, "healthy_nodes": int}, ...]`, sorted by
        model string. Advisory only — a node can disconnect between this
        answer and a later call, so it can never be a guarantee; a model
        that vanishes in between fails that hop like any other.

        Deliberately does NOT touch the flow's record: no content was sent
        to a node, so there is no hop and nobody saw anything. For the same
        reason a failure raises TransportError rather than HopError —
        there would be no Hop to attach and no fault to report. See the
        design doc for issue #56.

        Added here rather than in issue #59, which omitted it only because
        the wire request did not exist yet.
        """
        reply = await transport.request(
            self._coordinator_url,
            self._coordinator_cert,
            {"type": "list_models", "token": self._token},
            timeout,
        )
        if reply.get("type") != "models":
            raise transport.TransportError(
                f"unexpected response from coordinator: {reply!r}"
            )
        return reply["models"]
```

- [ ] **Step 4: Export `TransportError`**

In `src/mycelium/client/__init__.py`, add it to both the import and `__all__`, keeping the alphabetical order the file already uses. Extend the module docstring to note that a discovery failure surfaces as `TransportError` while a hop failure surfaces as `HopError`, so an agent author knows which to catch.

- [ ] **Step 5: Run and confirm they pass, then the whole suite**

Run: `pytest tests/client/test_flow.py -v` then `pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/client/flow.py src/mycelium/client/__init__.py tests/client/test_flow.py
git commit -m "feat: Flow.list_models, the half issue #59 deferred (#56)

Discovery is not a hop, so it does not touch the flow's record and a
failure raises TransportError rather than HopError — there would be no
Hop to attach and no fault to report. TransportError joins the package's
exports so an agent has one name to catch.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Document it

**Files:**
- Modify: `docs/OPERATIONS.md`

- [ ] **Step 1: Find the client section**

Run: `grep -n "Step 5 — Send a completion as a client" docs/OPERATIONS.md`

Read Step 5 in full, including the multi-turn and agent subsections added by #55 and #60.

- [ ] **Step 2: Add a subsection**

At the end of Step 5, after the reference-agent pointer, add:

````markdown
### Seeing which models are available

Rather than guessing a model string and finding out at call time:

```bash
mycelium-client-models \
  --coordinator-url wss://coordinator.example:8765 \
  --coordinator-cert coordinator.pem \
  --token-file client-token.txt
```

```
Qwen/Qwen2.5-1.5B-Instruct  1 node
Qwen/Qwen2.5-7B-Instruct    2 nodes
```

From an agent, `await flow.list_models()` returns the same thing as a list
of `{"model": ..., "healthy_nodes": ...}`.

This is **advisory, not a guarantee**. A volunteer can disconnect between
the answer and your next call, so a model listed here can still fail at
call time — the hop fails and surfaces to you, exactly as it would if you
had guessed the string.

It deliberately shows less than `mycelium-coordinator-status`: model
strings and counts only, never node fingerprints, the GitHub accounts
behind them, or reputation counters. Those are the operator's view, and a
client token should not be able to enumerate the people volunteering
hardware.
````

- [ ] **Step 3: Verify the documented command parses**

Run: `python -c "from mycelium.client.models_cli import parse_args; parse_args(['--coordinator-url','wss://x','--coordinator-cert','c.pem','--token-file','t.txt']); print('ok')"`
Expected: `ok`. A documented command that does not parse is worse than none.

- [ ] **Step 4: Commit and open the PR**

```bash
git add docs/OPERATIONS.md
git commit -m "docs: document client-facing model discovery (#56)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin phase-2/issue-56-model-discovery
```

Open the PR against `main`, titled `Client-facing model discovery (#56)`. The body should state: what the reply contains and, more importantly, what it deliberately omits and why; that the registry view is built up rather than filtered down, so future operator fields are not exposed by default; that discovery is advisory and can never be a guarantee; that `Flow.list_models()` is the half #59 deferred; and that this request stays client-gated whatever #68 decides about the operator surface.

---

## Acceptance criteria mapping

| #56 criterion | Where it is met |
|---|---|
| A client can list served models with a healthy-node count | Task 2 Step 1, Task 3 Step 1, Task 4 Step 1 |
| Two nodes on different models — both appear | Task 2 Step 1 (`..._reports_each_served_model_with_its_count`) |
| A model with no connected node does not appear | Task 1 Step 1 (`..._drops_a_model_whose_node_unregistered`) |
| No fingerprints, identities or reputation counters | Task 1 Step 1 and Task 2 Step 1, both asserting an exact key set |
| Gated by the same shared client token | Task 2 Step 1 (`..._with_a_wrong_token_is_closed_without_reply`) |
| `docs/OPERATIONS.md` documents it | Task 5 |

## Notes for whoever picks this up

- Do not build the client view by filtering `list_nodes()`. The point of a separate method is that a future operator-facing field is not exposed by default.
- Do not migrate `status_cli` or `ban_cli` onto the shared transport. Tempting while you are there, and out of scope.
- The exact-key-set assertions are the load-bearing ones. A check for absent fields passes against a reply carrying a field nobody thought to forbid.
- `list_models` stays gated on the client token regardless of what #68 decides for the operator surface — it is the example of what a client token should be able to do.
