# Issue #11 — Coordinator Re-routes When the Active Node Goes Down — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** With 2+ healthy nodes hosting the same model, the coordinator spreads requests across them and, if the node a request gets routed to turns out to be dead, automatically retries a different healthy node before failing — so killing the node that handled the last request never causes the next request to fail. This closes Phase 0 success criterion #3.

**Architecture:** Two localized changes on top of #10's request-routing path. `NodeRegistry.find_node_for_model` gains round-robin selection (per-model rotation state) and an `exclude` parameter. `server._handle_complete_request` wraps its existing single pick-and-route call in a bounded retry loop: a `NodeDisconnectedError` triggers an immediate registry self-heal (unregister) and a retry against a different node; a timeout or node-reported failure is surfaced immediately, never retried. No new modules, no wire protocol changes.

**Tech Stack:** Python 3.11+, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`, already configured), `websockets` 17.x (already pinned). No new dependencies.

## Global Constraints

- **Source of truth:** `docs/superpowers/specs/2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md`. Every decision below traces to a section there — if anything in this plan seems to contradict it, the design doc wins; stop and re-read it rather than guessing.
- Selection is round-robin, not first-match: `NodeRegistry` tracks, per model, the `node_id` it last returned, and the next call returns the next candidate after it, wrapping around. If that node has since left the registry, rotation restarts from the front of the current candidate list.
- New signature: `NodeRegistry.find_node_for_model(model: str, exclude: frozenset[str] = frozenset()) -> Node | None`. `exclude` skips node_ids without touching the cross-request rotation state.
- Failover triggers **only** on `router.NodeDisconnectedError`. `router.NodeTimeoutError` and `router.NodeError` are surfaced to the client immediately, never retried — a slow-but-alive node isn't retried, to avoid double-running a prompt on two nodes.
- The instant a routed request raises `NodeDisconnectedError` for a node, the coordinator unregisters it right there (`registry.unregister(node.node_id, node.websocket)`) instead of waiting for #9's ping/pong timeout (~50s worst case).
- Retry is bounded by the registry, not a fixed attempt count: each node that fails is added to a per-request `tried` set and excluded from later picks in that same request; the loop ends in success or once the registry has no more untried candidates (`router.NoHealthyNodeError`).
- No changes to `router.py`, the wire protocol, node registration, heartbeat/liveness, or the shared-token auth model (#8/#9/#10, unchanged).
- This issue's final task is **live-hardware verification** on two node agents on `a6000` pinned to different GPUs (`h100`'s permission gap for `bapi` is still unresolved as of #6's design doc, with no later doc recording a fix) — do not mark the issue done from simulated tests alone.

---

### Task 1: Round-robin selection in `NodeRegistry.find_node_for_model`

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Modify: `tests/coordinator/test_registry.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `NodeRegistry.find_node_for_model(model: str, exclude: frozenset[str] = frozenset()) -> Node | None` (Task 2 consumes this).

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_registry.py`, replace:

```python
def test_find_node_for_model_returns_first_match_when_multiple_host_same_model():
    registry = NodeRegistry("secret")
    registry.register("node-a", "model-a", websocket="ws-a")
    registry.register("node-b", "model-a", websocket="ws-b")
    node = registry.find_node_for_model("model-a")
    assert node.node_id == "node-a"
```

with:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_registry.py -v`
Expected: `test_find_node_for_model_round_robins_across_matching_nodes` and `test_find_node_for_model_round_robin_restarts_when_last_returned_node_is_gone` FAIL (current code always returns the first match, not a rotation). The two `exclude`-based tests FAIL with `TypeError: find_node_for_model() got an unexpected keyword argument 'exclude'`.

- [ ] **Step 3: Implement**

In `src/mycelium/coordinator/registry.py`, replace:

```python
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("token must not be empty")
        self._token = token
        self._nodes: dict[str, Node] = {}
```

with:

```python
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("token must not be empty")
        self._token = token
        self._nodes: dict[str, Node] = {}
        # model -> node_id this returned last, for round-robin selection in
        # find_node_for_model. See the design doc for issue #11.
        self._last_returned: dict[str, str] = {}
```

Then replace:

```python
    def find_node_for_model(self, model: str) -> Node | None:
        """Return the first registered node hosting `model`, or None. No
        load balancing across same-model nodes — see the design doc for
        issue #10: Phase 0 doesn't need fairness, just a healthy match."""
        for node in self._nodes.values():
            if node.model == model:
                return node
        return None
```

with:

```python
    def find_node_for_model(
        self, model: str, exclude: frozenset[str] = frozenset()
    ) -> Node | None:
        """Return the next registered node hosting `model`, round-robin
        across current candidates (skipping any node_id in `exclude`), or
        None if none match — see the design doc for issue #11.

        Rotation state is the node_id this returned last time for `model`;
        the next call returns the candidate after it, wrapping around. If
        that node has since left the registry (or is itself excluded),
        rotation restarts from the front of the current candidate list —
        no fairness guarantee across registry churn, only "don't always
        pick the same node when several are healthy."

        `exclude` lets a caller retry with a different node within one
        client request (see server.py's failover loop) without disturbing
        the cross-request rotation state.
        """
        candidates = [
            node
            for node in self._nodes.values()
            if node.model == model and node.node_id not in exclude
        ]
        if not candidates:
            return None

        start = 0
        last = self._last_returned.get(model)
        if last is not None:
            ids = [node.node_id for node in candidates]
            if last in ids:
                start = (ids.index(last) + 1) % len(candidates)

        node = candidates[start]
        self._last_returned[model] = node.node_id
        return node
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_registry.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS. `find_node_for_model`'s only other caller, `server._handle_complete_request`, still calls it with a single positional argument, which the new default-valued `exclude` parameter doesn't break.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/coordinator/registry.py tests/coordinator/test_registry.py
git commit -m "feat: registry round-robins across healthy nodes for a model"
```

---

### Task 2: Bounded failover loop in `server._handle_complete_request`

**Files:**
- Modify: `src/mycelium/coordinator/server.py`
- Modify: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `NodeRegistry.find_node_for_model(model, exclude=...)` from Task 1; `router.NodeDisconnectedError`, `router.RoutingError`, `router.NoHealthyNodeError` (all already exist in `router.py`, unchanged).
- Produces: nothing new consumed elsewhere — this is the top of the call chain for this behavior.

- [ ] **Step 1: Write the failing tests**

In `tests/coordinator/test_server.py`, add to the imports:

```python
from mycelium.coordinator.registry import NodeRegistry
```

so the import block reads:

```python
from mycelium.coordinator import certs, router, server
from mycelium.coordinator.registry import NodeRegistry
```

After the existing `_run_fake_node` helper (right before `test_complete_request_routes_to_registered_node_and_returns_result`), add two fake-object helpers for testing `_handle_complete_request` directly, without a real network connection — the same technique `tests/coordinator/test_router.py` already uses for `route_request`:

```python
class _FakeNodeWebsocket:
    """Stand-in for a node's websocket, for testing
    server._handle_complete_request's failover logic in isolation from a
    real network connection — mirrors test_router.py's _FakeNodeWebsocket."""

    def __init__(self, send_raises=None):
        self.sent: list[str] = []
        self._send_raises = send_raises

    async def send(self, raw: str) -> None:
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append(raw)


class _FakeClientWebsocket:
    """Collects what _handle_complete_request sends back to the client,
    without a real network connection."""

    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        self.closed = True
```

At the end of the file, add:

```python
async def test_complete_request_round_robins_across_two_healthy_nodes(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_a_ws:
            await node_a_ws.send(json.dumps(
                {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-a"}
            ))
            await node_a_ws.recv()
            node_a_task = asyncio.create_task(_run_fake_node(
                node_a_ws, lambda msg: {"type": "complete_result", "text": f"node-a: {msg['prompt']}"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b_ws:
                await node_b_ws.send(json.dumps(
                    {"type": "register", "token": "secret-token", "model": "m", "node_id": "node-b"}
                ))
                await node_b_ws.recv()
                node_b_task = asyncio.create_task(_run_fake_node(
                    node_b_ws, lambda msg: {"type": "complete_result", "text": f"node-b: {msg['prompt']}"}
                ))

                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                    await client_ws.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "1"}
                    ))
                    first = json.loads(await client_ws.recv())
                    await client_ws.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "2"}
                    ))
                    second = json.loads(await client_ws.recv())

                assert {first["text"], second["text"]} == {"node-a: 1", "node-b: 2"}

            node_a_task.cancel()
            node_b_task.cancel()


async def test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead():
    registry = NodeRegistry("secret-token")
    dead_ws = _FakeNodeWebsocket(
        send_raises=websockets.exceptions.ConnectionClosedError(None, None)
    )
    healthy_ws = _FakeNodeWebsocket()
    registry.register("node-a", "m", dead_ws)  # registers first -> round robin picks it first
    registry.register("node-b", "m", healthy_ws)

    async def reply_from_node_b():
        while not healthy_ws.sent:
            await asyncio.sleep(0.01)
        sent = json.loads(healthy_ws.sent[0])
        node_b = registry.get("node-b")
        node_b.pending[sent["request_id"]].set_result(
            {"type": "complete_result", "text": "answer from node-b", "request_id": sent["request_id"]}
        )

    asyncio.create_task(reply_from_node_b())

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    assert json.loads(client_ws.sent[0]) == {"type": "complete_result", "text": "answer from node-b"}
    # node-a's dead connection must have been self-healed out of the registry.
    assert registry.list_nodes() == [{"node_id": "node-b", "model": "m"}]


async def test_complete_request_does_not_fail_over_on_timeout(monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 0.2)
    registry = NodeRegistry("secret-token")
    slow_ws = _FakeNodeWebsocket()  # accepts the send, never replies
    other_ws = _FakeNodeWebsocket()
    registry.register("node-a", "m", slow_ws)
    registry.register("node-b", "m", other_ws)

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    response = json.loads(client_ws.sent[0])
    assert response["type"] == "complete_error"
    assert other_ws.sent == []  # node-b was never contacted
    # A timeout isn't treated as a dead node — node-a stays registered.
    assert registry.list_nodes() == [
        {"node_id": "node-a", "model": "m"},
        {"node_id": "node-b", "model": "m"},
    ]


async def test_complete_request_returns_error_when_every_node_is_dead():
    registry = NodeRegistry("secret-token")
    dead_a = _FakeNodeWebsocket(send_raises=websockets.exceptions.ConnectionClosedError(None, None))
    dead_b = _FakeNodeWebsocket(send_raises=websockets.exceptions.ConnectionClosedError(None, None))
    registry.register("node-a", "m", dead_a)
    registry.register("node-b", "m", dead_b)

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    response = json.loads(client_ws.sent[0])
    assert response["type"] == "complete_error"
    assert registry.list_nodes() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/coordinator/test_server.py -v`
Expected: `test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead` and `test_complete_request_returns_error_when_every_node_is_dead` FAIL — the current `_handle_complete_request` has no retry loop, so node-a's `NodeDisconnectedError` propagates straight out to a `complete_error` reply instead of failing over to node-b. `test_complete_request_round_robins_across_two_healthy_nodes` and `test_complete_request_does_not_fail_over_on_timeout` should already PASS at this point — both only depend on Task 1's round-robin registry and a single `find_node_for_model` call, which the current (pre-Task-2) `_handle_complete_request` already does; they exist here to lock in that this task's retry loop must not change that already-correct behavior.

- [ ] **Step 3: Implement**

In `src/mycelium/coordinator/server.py`, replace:

```python
async def _handle_complete_request(websocket, registry: NodeRegistry, message: dict) -> None:
    """A client's one-shot completion request: authenticate, pick a
    healthy node hosting the requested model, forward, relay the result
    (or a clear error) back, then close — see the design doc for issue
    #10."""
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

    try:
        node = registry.find_node_for_model(model)
        if node is None:
            raise router.NoHealthyNodeError(f"no healthy node for model {model!r}")
        text = await router.route_request(node, prompt)
    except router.RoutingError as exc:
        try:
            await websocket.send(json.dumps({"type": "complete_error", "reason": str(exc)}))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.close()
        return

    try:
        await websocket.send(json.dumps({"type": "complete_result", "text": text}))
    except websockets.exceptions.ConnectionClosed:
        return
    await websocket.close()
```

with:

```python
async def _handle_complete_request(websocket, registry: NodeRegistry, message: dict) -> None:
    """A client's one-shot completion request: authenticate, pick a
    healthy node hosting the requested model, forward, relay the result
    (or a clear error) back, then close — see the design doc for issue
    #10. If the picked node turns out to be disconnected, self-heal the
    registry and retry a different healthy node before giving up — see
    the design doc for issue #11. A timeout or a node-reported failure is
    not retried: the node might still be working, and silently re-running
    the same prompt on a second node risks double-executing it."""
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

    tried: set[str] = set()
    while True:
        try:
            node = registry.find_node_for_model(model, exclude=frozenset(tried))
            if node is None:
                raise router.NoHealthyNodeError(f"no healthy node for model {model!r}")
            text = await router.route_request(node, prompt)
        except router.NodeDisconnectedError:
            # The picked node is actually dead — self-heal the registry
            # right away (don't wait for #9's ping/pong timeout) and try a
            # different healthy node instead of failing the request.
            registry.unregister(node.node_id, node.websocket)
            tried.add(node.node_id)
            continue
        except router.RoutingError as exc:
            try:
                await websocket.send(json.dumps({"type": "complete_error", "reason": str(exc)}))
            except websockets.exceptions.ConnectionClosed:
                return
            await websocket.close()
            return
        else:
            break

    try:
        await websocket.send(json.dumps({"type": "complete_result", "text": text}))
    except websockets.exceptions.ConnectionClosed:
        return
    await websocket.close()
```

`except router.NodeDisconnectedError` must stay above `except router.RoutingError` — `NodeDisconnectedError` is a subclass of `RoutingError`, so the more specific clause has to come first or it would never be reached.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/coordinator/test_server.py -v`
Expected: all PASS, including all four new tests.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS. In particular, re-check `test_complete_request_routes_to_registered_node_and_returns_result` and the other pre-existing single-node `_handle_complete_request` tests in this file still pass — with only one node registered, round-robin always returns that same node, so this task doesn't change single-node behavior.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "feat: coordinator fails over to a different node when the picked one is disconnected"
```

---

### Task 3: Update `docs/phases/phase-0-foundation.md`

**Files:**
- Modify: `docs/phases/phase-0-foundation.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed elsewhere — documentation only.

- [ ] **Step 1: Update the `Related:` line**

Replace:

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md) · [Node heartbeat & liveness tracking](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md) · [Coordinator forwards a client request](../superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md)
```

with:

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md) · [Node heartbeat & liveness tracking](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md) · [Coordinator forwards a client request](../superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md) · [Coordinator re-routes when the active node goes down](../superpowers/specs/2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md)
```

- [ ] **Step 2: Add a new bullet to `## Open risks / unresolved decisions`**

After the `**Node heartbeat/liveness: resolved.**` bullet, add:

```markdown
- **Multi-node failover: resolved.** With 2+ healthy nodes hosting the same model, the coordinator round-robins across them (`NodeRegistry.find_node_for_model`, per-model rotation state) rather than always picking the same one. If the node a request gets routed to turns out to be disconnected, the coordinator self-heals the registry immediately (doesn't wait for #9's ping/pong timeout) and retries a different healthy node before failing — so killing the node that handled the last request never produces a client-visible failure as long as another healthy node exists. A timeout or a node-reported failure is surfaced immediately rather than retried, to avoid double-running a prompt on two nodes. See [the design doc](../superpowers/specs/2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md) for the full decision record.
```

- [ ] **Step 3: Update the `## Next step` section**

Replace:

```markdown
## Next step

Phase 0's core happy path (client → coordinator → node → vLLM → response) is implemented and live-verified as of issue #10 — see success criterion #2 above. #9 already covers success criterion #3 (killing a node removes it from routing). Issues #11 ("Coordinator re-routes when the active node goes down") and #12 ("Clean, immediate failure when no healthy node is available") remain open and refine adjacent behavior further; #10 itself already fails a request immediately with a clear error when `find_node_for_model` finds no match (success criterion #4's basic case), so #12's scope should be checked against that before assuming it's starting from nothing.
```

with:

```markdown
## Next step

Phase 0's core happy path (client → coordinator → node → vLLM → response) is implemented and live-verified as of issue #10. #9 covers a single dead node being dropped from routing; #11 extends that to the multi-node case — round-robin selection plus immediate failover when the currently-picked node is the one that just died — closing success criterion #3 in full. Issue #12 ("Clean, immediate failure when no healthy node is available") remains open; #10 already fails a request immediately with a clear error when no node at all is registered for a model, and #11's registry-exhausted path (every candidate for a model tried and dead) fails the same way, so #12's scope should be checked against both before assuming it's starting from nothing.
```

- [ ] **Step 4: Commit**

```bash
git add docs/phases/phase-0-foundation.md
git commit -m "docs: resolve issue #11's open risk in the phase-0 foundation doc"
```

---

### Task 4: Live-hardware verification

**Files:** none (verification only — the design doc gets a `## Live verification` section appended afterward, see Step 4 below).

**Interfaces:** none — this task exercises the already-implemented, already-tested system as a real operator would.

- [ ] **Step 1: Start a real coordinator and two real node agents on `a6000`, pinned to different GPUs**

On the coordinator host (localhost, matching #10's live-verification setup):

```bash
openssl rand -hex 32 > token.txt
mycelium-coordinator --token-file token.txt --cert-san-ip <coordinator-ip>
```

On `a6000`, in two separate terminals/sessions (check `nvidia-smi` first and pick two currently-idle GPU indices — #10's live run used GPU 2 while 0/1/3 carried other users' workloads on this shared machine; pick whichever two are idle at run time):

```bash
mycelium-node --coordinator-url wss://<coordinator-ip>:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt \
    --node-id a6000-node-a --gpu <idle-gpu-index-1>
```

```bash
mycelium-node --coordinator-url wss://<coordinator-ip>:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt \
    --node-id a6000-node-b --gpu <idle-gpu-index-2>
```

Confirm both registered via `mycelium-coordinator-status` — expect two entries, both hosting `Qwen/Qwen2.5-7B-Instruct`.

- [ ] **Step 2: Scenario A — the literal acceptance criterion**

```bash
mycelium-client --coordinator-url wss://<coordinator-ip>:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt \
    --model Qwen/Qwen2.5-7B-Instruct --prompt "What is the capital of France?"
```

Note which node served it (there's no client-visible indicator of this — cross-reference against each node agent's own terminal output, which logs when it receives a routed request). On the machine running that node's process, find its PID (`pgrep -f "mycelium-node.*<that node's --node-id>"` or check the terminal it's running in) and:

```bash
kill -9 <that node's PID>
```

Confirm via `nvidia-smi` that its GPU returns to idle (no orphaned vLLM worker processes). Then immediately send a second request:

```bash
mycelium-client --coordinator-url wss://<coordinator-ip>:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt \
    --model Qwen/Qwen2.5-7B-Instruct --prompt "What is the capital of Japan?"
```

Expected: a correct completion (mentioning Tokyo), served via the *other* node, with no error surfaced to the client.

- [ ] **Step 3: Scenario B — forced failover**

Restart whichever node agent was killed in Step 2, so two healthy nodes are registered again. This time, `kill -9` one node's process *before* sending any request at all (pick whichever registered first, since round-robin's first-ever pick for a fresh rotation state is registration order). Confirm via `nvidia-smi` its GPU is idle. Then send a request:

```bash
mycelium-client --coordinator-url wss://<coordinator-ip>:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt \
    --model Qwen/Qwen2.5-7B-Instruct --prompt "Say the word banana and nothing else."
```

Expected: the coordinator's first pick is the already-dead node, so this specifically exercises the disconnect-catch/self-heal/retry path (not just round-robin luck) — the request should still succeed via the surviving node, with the same `banana` completion and no client-visible failure. Confirm via `mycelium-coordinator-status` that the dead node no longer appears (self-healed out immediately, not left registered until a heartbeat timeout).

- [ ] **Step 4: Record the verification in the design doc**

Append to `docs/superpowers/specs/2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md`, after the `## Explicitly out of scope for this issue` section, a `## Live verification` section — following the exact narrative style of issue #10's design doc's own `## Live verification` section (real commands run, real output observed, which acceptance criteria each observation closes). Fill in the actual hardware/GPU indices used, actual commands run, and actual output observed during Steps 1–3 above — do not write placeholder/hypothetical output.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md
git commit -m "docs: live-hardware verification for issue #11"
```
