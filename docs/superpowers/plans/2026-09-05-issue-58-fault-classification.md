# Client-Caused Faults Must Not Damage Node Reputation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A client sending a request the model can't accept — most obviously context exceeding the window — gets a clear, actionable error, and the volunteer node that relayed it keeps its reputation untouched.

**Architecture:** `vllm_process` owns the HTTP boundary and raises `VLLMClientError` or `VLLMServerError` carrying vLLM's own message. `request_handler` maps those to a `fault` field on the wire. `router` raises `ClientRequestError` — a `RoutingError` that is deliberately not a `NodeError` — so no existing handler can record a crash for it. The coordinator relays it without touching reputation and without failing over, while recording a separate `client_faults` counter so a node that claims client fault constantly is visible to the operator.

**Tech Stack:** Python 3.11+, stdlib `urllib`/`json`, `websockets` 17.0.1, `pytest` 9.1.1 with `pytest-asyncio` in `asyncio_mode = "auto"`.

## Global Constraints

- Issue: [#58](https://github.com/Zenkai-Dynamics/Mycelium/issues/58). Parent: [#54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54).
- Design authority: `docs/superpowers/specs/2026-09-05-issue-58-fault-classification-design.md`, and behind it `docs/superpowers/specs/2026-09-04-phase-2-implementation-design.md`. Where this plan and those disagree, the documents win.
- Branch: `phase-2/issue-58-fault-classification`, off `main`. One PR at the end.
- **Fault classification:** 4xx is the client's fault **except 404, 408 and 429**, which are node faults. 5xx and transport failures are node faults.
- **Anything not exactly the string `"client"` is a node fault** — including an absent field and an unrecognized value. The permissive direction is the exploitable one.
- The `client_faults` counter must **never** influence `_reputation_weight()`. It is operator-visible only; letting it affect routing would recreate the bug this issue fixes.
- A client fault still counts as exposure — the node received the conversation and vLLM read it. It appends to `exposed` like any other outcome.
- The node never truncates a caller's context to make it fit.
- No test may make a real network call to GitHub or start a real vLLM. Fakes only.
- Style: match the surrounding code — long docstrings recording *why*, comments citing the issue number whose design doc explains a decision.
- Run the whole suite (`pytest`) before every commit. Every commit leaves it green. The suite is currently at 292 passing.

---

### Task 1: `vllm_process` raises typed errors

**Files:**
- Modify: `src/mycelium/node/vllm_process.py`
- Test: `tests/node/test_vllm_process.py`

**Interfaces:**
- Produces: `vllm_process.VLLMClientError`, `vllm_process.VLLMServerError`, `vllm_process.NODE_FAULT_STATUSES = frozenset({404, 408, 429})`. `complete()` raises one of the two for any `urllib.error.HTTPError`; non-HTTP failures (transport, timeout) propagate unchanged and are treated as node faults by the caller.

- [ ] **Step 1: Give the fake vLLM server a way to return errors**

`tests/node/test_vllm_process.py` has a module-level `RECEIVED_BODIES` list that `_FakeVLLMHandler.do_POST` appends to. Add a second module-level override beside it, following the same pattern (the server runs in its own thread, so a test sets it before driving `VLLMProcess.complete`):

```python
# Set by a test to make the next /v1/chat/completions call fail with a
# chosen status and body; cleared by the fake_vllm_server fixture. A dict
# rather than a module-level rebind so the handler can read it without a
# `global` declaration, matching RECEIVED_BODIES above.
RESPONSE_OVERRIDE: dict = {}
```

In `_FakeVLLMHandler.do_POST`, immediately after the line that appends to `RECEIVED_BODIES`, insert:

```python
            if RESPONSE_OVERRIDE:
                body = RESPONSE_OVERRIDE["body"]
                self.send_response(RESPONSE_OVERRIDE["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
```

and in the `fake_vllm_server` fixture, clear both before yielding so no test leaks state into another:

```python
@pytest.fixture
def fake_vllm_server():
    RECEIVED_BODIES.clear()
    RESPONSE_OVERRIDE.clear()
    server = HTTPServer(("127.0.0.1", 0), _FakeVLLMHandler)
```

- [ ] **Step 2: Write the failing tests**

```python
def _overflow_body() -> bytes:
    """The shape vLLM actually returns for a context-window overflow —
    `message` at the top level, not nested under `error` as some
    OpenAI-compatible servers do."""
    return json.dumps({
        "object": "error",
        "message": "This model's maximum context length is 32768 tokens, "
                   "however you requested 41022 tokens",
        "type": "BadRequestError",
        "code": 400,
    }).encode()


def test_context_overflow_raises_client_error_with_vllm_message(fake_vllm_server):
    RESPONSE_OVERRIDE.update({"status": 400, "body": _overflow_body()})
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMClientError) as exc:
        process.complete([{"role": "user", "content": "hi"}])

    assert "maximum context length" in str(exc.value)
    assert "41022" in str(exc.value), (
        "the requested size is what makes an overflow actionable — it must survive"
    )


def test_model_not_found_is_a_node_fault_despite_being_4xx(fake_vllm_server):
    """404 means the node registered a model its vLLM isn't serving."""
    RESPONSE_OVERRIDE.update({
        "status": 404,
        "body": json.dumps({"message": "The model `other` does not exist"}).encode(),
    })
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMServerError):
        process.complete([{"role": "user", "content": "hi"}])


def test_overloaded_statuses_are_node_faults(fake_vllm_server):
    """408 and 429 are the node's capacity, not a defect in the request."""
    for status in (408, 429):
        RESPONSE_OVERRIDE.clear()
        RESPONSE_OVERRIDE.update({
            "status": status, "body": json.dumps({"message": "busy"}).encode(),
        })
        process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

        with pytest.raises(vllm_process.VLLMServerError):
            process.complete([{"role": "user", "content": "hi"}])


def test_server_error_is_a_node_fault(fake_vllm_server):
    RESPONSE_OVERRIDE.update({
        "status": 500, "body": json.dumps({"message": "engine died"}).encode(),
    })
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMServerError) as exc:
        process.complete([{"role": "user", "content": "hi"}])

    assert "engine died" in str(exc.value)


def test_unparseable_error_body_falls_back_to_the_raw_text(fake_vllm_server):
    """An unexpected error shape must still produce something actionable."""
    RESPONSE_OVERRIDE.update({"status": 400, "body": b"<html>Bad Request</html>"})
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMClientError) as exc:
        process.complete([{"role": "user", "content": "hi"}])

    assert "Bad Request" in str(exc.value)


def test_nested_error_message_shape_is_also_read(fake_vllm_server):
    RESPONSE_OVERRIDE.update({
        "status": 400,
        "body": json.dumps({"error": {"message": "too many tokens"}}).encode(),
    })
    process = VLLMProcess(model="m", port=fake_vllm_server.server_address[1])

    with pytest.raises(vllm_process.VLLMClientError) as exc:
        process.complete([{"role": "user", "content": "hi"}])

    assert "too many tokens" in str(exc.value)
```

Add `from mycelium.node import vllm_process` to the imports if the file references classes bare; it already imports `VLLMProcess` and `VLLMReadyTimeout` directly, so import the two new classes the same way if you prefer — just be consistent with the file.

- [ ] **Step 3: Run and confirm they fail**

Run: `pytest tests/node/test_vllm_process.py -k "client_error or node_fault or overloaded or server_error or fallback or nested" -v`
Expected: FAIL — `module 'mycelium.node.vllm_process' has no attribute 'VLLMClientError'`.

- [ ] **Step 4: Implement**

In `src/mycelium/node/vllm_process.py`, add below `VLLMReadyTimeout`:

```python
# 4xx codes that are the NODE's fault despite being client-error codes:
# 404 means vLLM isn't serving the model this node registered (a
# misconfiguration), and 408/429 mean the node is too busy to serve.
# Classifying these as client faults would shield a node that genuinely
# cannot serve from ever reflecting it in its reputation — the mirror
# image of the bug issue #58 exists to fix. See the design doc for #58.
NODE_FAULT_STATUSES = frozenset({404, 408, 429})


class VLLMClientError(Exception):
    """vLLM rejected the request itself — most often context exceeding the
    model's window. The caller's mistake, not the node's, so it must never
    be recorded against this node's reputation. See the design doc for
    issue #58."""


class VLLMServerError(Exception):
    """vLLM failed for a reason that is the node's own — a 5xx, or one of
    NODE_FAULT_STATUSES. Recorded against reputation exactly as any other
    node failure."""


def _error_message(body: bytes, status: int) -> str:
    """Pull the human-readable message out of vLLM's error body.

    vLLM returns `message` at the top level; some OpenAI-compatible
    servers nest it under `error`. Both are read, and anything
    unparseable falls back to the raw text — an unexpected error shape
    must still produce something a caller can act on rather than an empty
    string. See the design doc for issue #58.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        message = parsed.get("message")
        if isinstance(message, str) and message:
            return message
        error = parsed.get("error")
        if isinstance(error, dict):
            nested = error.get("message")
            if isinstance(nested, str) and nested:
                return nested
    text = body.decode("utf-8", errors="replace").strip()
    return text or f"vLLM returned HTTP {status} with no message"
```

Then wrap the request in `complete`:

```python
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # Only HTTP responses are classified here. Transport failures
            # (URLError, socket timeouts) propagate unchanged and are
            # treated as node faults by request_handler's catch-all —
            # there is no client-caused way to make the local loopback
            # connection to vLLM fail. See the design doc for issue #58.
            message = _error_message(exc.read(), exc.code)
            if 400 <= exc.code < 500 and exc.code not in NODE_FAULT_STATUSES:
                raise VLLMClientError(message) from exc
            raise VLLMServerError(message) from exc
        return body["choices"][0]["message"]["content"]
```

Note `urllib.error.HTTPError` is a subclass of `URLError`, so this `except` must not be widened to `URLError` or the classification would swallow transport failures too.

- [ ] **Step 5: Run and confirm they pass, then the whole suite**

Run: `pytest tests/node/test_vllm_process.py -v` then `pytest`
Expected: PASS. Existing behavior is unchanged for successful calls, and the node's broad `except Exception` still turns either new error into a `complete_error`.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/node/vllm_process.py tests/node/test_vllm_process.py
git commit -m "feat: classify vLLM errors as client- or node-caused (#58)

4xx is the client's fault except 404, 408 and 429 — a model the node
isn't serving, and two flavours of the node being too busy, are the
volunteer's problem rather than a defect in the request. vLLM's own
message is carried through so a context overflow names the limit and the
size requested instead of surfacing as HTTP Error 400.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The node reports `fault` on the wire

**Files:**
- Modify: `src/mycelium/node/request_handler.py`
- Test: `tests/node/test_request_handler.py`

**Interfaces:**
- Consumes: `VLLMClientError` from Task 1.
- Produces: node→coordinator `complete_error` gains `"fault": "client" | "node"`.

- [ ] **Step 1: Write the failing tests**

`tests/node/test_request_handler.py` has a `_FakeProcess` whose `complete` raises a configured error. Add:

```python
async def test_client_error_is_reported_as_a_client_fault():
    process = _FakeProcess(error=VLLMClientError("context too long"))
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "abc",
                    "messages": [{"role": "user", "content": "hi"}]})
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)  # let the spawned per-message task finish and reply
    websocket.close_from_test()
    await handler_task

    reply = json.loads(websocket.sent[0])
    assert reply == {
        "type": "complete_error", "request_id": "abc",
        "reason": "context too long", "fault": "client",
    }


async def test_server_error_is_reported_as_a_node_fault():
    process = _FakeProcess(error=VLLMServerError("engine died"))
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "abc",
                    "messages": [{"role": "user", "content": "hi"}]})
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    reply = json.loads(websocket.sent[0])
    assert reply["fault"] == "node"


async def test_an_unexpected_exception_is_a_node_fault():
    """Anything that isn't explicitly a client error defaults to the
    node's fault — the conservative direction."""
    process = _FakeProcess(error=RuntimeError("something else broke"))
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "abc",
                    "messages": [{"role": "user", "content": "hi"}]})
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)
    websocket.close_from_test()
    await handler_task

    reply = json.loads(websocket.sent[0])
    assert reply["fault"] == "node"
```

Import the two error classes at the top of the file. The existing `test_handle_messages_replies_with_error_when_complete_raises` asserts an exact dict without `fault` — update it to include `"fault": "node"`, since a `RuntimeError` is now explicitly a node fault.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/node/test_request_handler.py -v`
Expected: FAIL — replies carry no `fault` key.

- [ ] **Step 3: Implement**

In `src/mycelium/node/request_handler.py`, import the errors and split the except:

```python
from mycelium.node.vllm_process import VLLMClientError, VLLMProcess
```

```python
    try:
        text = await asyncio.to_thread(process.complete, messages)
    except VLLMClientError as exc:
        # The caller's own request was unacceptable to the model — most
        # often context exceeding its window. Flagged so the coordinator
        # can relay it without recording a crash against this node, which
        # did nothing wrong. See the design doc for issue #58.
        reply = {
            "type": "complete_error", "request_id": request_id,
            "reason": str(exc), "fault": "client",
        }
    except Exception as exc:
        # Broad except is deliberate here, not sloppy: whatever else goes
        # wrong calling vLLM (transport failure, timeout, malformed
        # response) becomes a complete_error the coordinator/client can
        # see, per the design doc for issue #10 — never left to hang or
        # crash this task. Everything reaching here is the node's own
        # fault by definition, since the one client-caused case is caught
        # above.
        reply = {
            "type": "complete_error", "request_id": request_id,
            "reason": str(exc), "fault": "node",
        }
    else:
        reply = {"type": "complete_result", "request_id": request_id, "text": text}
```

- [ ] **Step 4: Run and confirm, then the whole suite**

Run: `pytest tests/node/ -v` then `pytest`
Expected: PASS. The coordinator ignores the unknown `fault` field for now; Task 4 gives it meaning.

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/request_handler.py tests/node/test_request_handler.py
git commit -m "feat: node reports whether a failure was the client's fault (#58)

Anything that is not explicitly a VLLMClientError is reported as the
node's own fault — the conservative direction, since a node must not be
able to dodge reputation by failing in an unrecognized way.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The `client_faults` counter

**Files:**
- Modify: `src/mycelium/coordinator/registry.py`
- Modify: `src/mycelium/coordinator/status_cli.py`
- Test: `tests/coordinator/test_registry.py`, `tests/coordinator/test_status_cli.py`

**Interfaces:**
- Produces: `ReputationCounters.client_faults`, `NodeRegistry.record_client_fault(public_key)`, `client_faults` in every `list_nodes()` reputation dict, and `client_faults:N` in the status line.

The counter exists because the node making the claim is untrusted. It must **not** feed `_reputation_weight()` — see the design doc.

- [ ] **Step 1: Update the assertions the new field breaks**

Several tests assert a complete reputation dict and will break the moment a fifth key exists. Find them all first:

Run: `grep -rn '"completions":' tests/ | grep -v client_faults`

Add `"client_faults": 0` to each literal reputation dict in `tests/coordinator/test_registry.py` (expected `list_nodes()` output) and in `tests/coordinator/test_status_cli.py` (input fixtures the CLI renders — the CLI will read the key, so a fixture missing it would raise `KeyError`). Do not change anything else about those tests.

Run `pytest tests/coordinator/test_registry.py tests/coordinator/test_status_cli.py -v` after this step: it will still fail, because the field does not exist yet. That is expected — this step and Step 2 land together.

- [ ] **Step 2: Write the failing tests for the new behavior**

In `tests/coordinator/test_registry.py`:

```python
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
```

The second test reaches into a private method deliberately: routing weight has no public accessor, and asserting the weight directly is far more precise than inferring it from a statistical sample of `find_node_for_model` draws. If the file already has a precedent for testing `_reputation_weight`, follow it.

In `tests/coordinator/test_status_cli.py`, extend the existing rendering assertion — find the test asserting `"node-a [a1b2c3d4e5f6] (github:octocat) [ok:12 timeout:1 crash:0 disconnect:2]: "` and update the expected string to include the new counter in the same bracket, matching whatever value its fixture supplies.

- [ ] **Step 3: Run and confirm they fail**

Run: `pytest tests/coordinator/test_registry.py tests/coordinator/test_status_cli.py -v`
Expected: FAIL — no `client_faults` key, no `record_client_fault`.

- [ ] **Step 4: Implement**

In `src/mycelium/coordinator/registry.py`, add the field to `ReputationCounters`:

```python
    disconnects: int = 0
    # Times this node claimed a failure was the CLIENT's fault (issue
    # #58). Deliberately NOT part of _reputation_weight: a client's
    # mistake must never move routing, which is the bug #58 fixes. It is
    # counted at all because the claim is self-reported by an untrusted
    # volunteer — a node that always claims "client" would otherwise
    # accrue no signal whatsoever. This makes the claim observable to the
    # operator without letting it influence anything.
    client_faults: int = 0
```

Add `"client_faults": counters.client_faults,` to `_reputation_dict`, and the recorder beside the others:

```python
    def record_client_fault(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).client_faults += 1
```

Leave `_reputation_weight` **unchanged** — its `total` must keep summing only completions, timeouts, crashes and disconnects. Add a line to its docstring saying client faults are excluded and why.

In `src/mycelium/coordinator/status_cli.py`, extend the rendered bracket:

```python
        reputation = (
            f" [ok:{rep['completions']} timeout:{rep['timeouts']} "
            f"crash:{rep['crashes']} disconnect:{rep['disconnects']} "
            f"client_faults:{rep['client_faults']}]"
        )
```

- [ ] **Step 5: Run the whole suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/mycelium/coordinator/registry.py src/mycelium/coordinator/status_cli.py \
        tests/coordinator/test_registry.py tests/coordinator/test_status_cli.py
git commit -m "feat: count client-fault claims per node, visibly (#58)

The claim is self-reported by an untrusted volunteer, so a node that
always claims 'client' would accrue no reputation signal at all. The
counter does not feed routing — that would recreate the bug — but it
appears in mycelium-coordinator-status, turning an unverifiable
assertion into an observable one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The coordinator stops recording crashes for client faults

**Files:**
- Modify: `src/mycelium/coordinator/router.py`
- Modify: `src/mycelium/coordinator/server.py`
- Test: `tests/coordinator/test_router.py`, `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `fault` on the node wire (Task 2), `record_client_fault` (Task 3).
- Produces: `router.ClientRequestError(RoutingError)`; client-facing `complete_error` gains `fault`.

- [ ] **Step 1: Write the failing router test**

```python
async def test_client_fault_reply_raises_client_request_error():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {
        "type": "complete_error", "reason": "context too long", "fault": "client",
    }))

    with pytest.raises(router.ClientRequestError, match="context too long"):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_client_request_error_is_not_a_node_error():
    """It must not be catchable as a NodeError, or an existing handler
    would record a crash for it — the bug issue #58 fixes."""
    assert not issubclass(router.ClientRequestError, router.NodeError)
    assert issubclass(router.ClientRequestError, router.RoutingError)


async def test_an_unrecognized_fault_value_is_a_node_error():
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {
        "type": "complete_error", "reason": "boom", "fault": "banana",
    }))

    with pytest.raises(router.NodeError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)


async def test_a_missing_fault_value_is_a_node_error():
    """An older node build predates the field; its failures must keep
    counting exactly as they do today."""
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {
        "type": "complete_error", "reason": "boom",
    }))

    with pytest.raises(router.NodeError):
        await router.route_request(node, [{"role": "user", "content": "hi"}], timeout=2.0)
```

- [ ] **Step 2: Run and confirm they fail, then implement the router**

Run: `pytest tests/coordinator/test_router.py -v`
Expected: FAIL — no `ClientRequestError`.

Add the class after `NodeError` in `src/mycelium/coordinator/router.py`:

```python
class ClientRequestError(RoutingError):
    """The node reported that the CLIENT's request was at fault — most
    often context exceeding the model's window.

    Deliberately NOT a subclass of NodeError: an existing `except
    NodeError` site records a crash against the node, and a client's own
    mistake must never do that. See the design doc for issue #58.

    Raised only when the reply says exactly "client". An absent or
    unrecognized value is a NodeError, so a node cannot dodge reputation
    by sending garbage in place of a valid claim.
    """
```

and change the tail of `route_request`:

```python
    if message.get("fault") == "client":
        raise ClientRequestError(
            message.get("reason", "the model rejected the request with no reason given")
        )
    raise NodeError(message.get("reason", "node reported a failure with no reason given"))
```

- [ ] **Step 3: Write the failing server tests**

These follow `test_complete_request_node_failure_increments_crash_counter`, which registers a fake node, drives one request, then reads counters back with a `status_query` on a third connection.

```python
_CLIENT_FAULT_REPLY = {
    "type": "complete_error",
    "reason": "This model's maximum context length is 32768 tokens, "
              "however you requested 41022 tokens",
    "fault": "client",
}


async def test_client_fault_leaves_reputation_untouched(tmp_path):
    """The defect this issue exists to fix: a client sending too much
    context must not damage an innocent volunteer's reputation."""
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
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(
                _run_fake_node(node_ws, lambda msg: dict(_CLIENT_FAULT_REPLY))
            )

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                await client_ws.recv()

            node_task.cancel()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps(
                    {"type": "status_query", "token": "secret-token"}
                ))
                response = json.loads(await status_ws.recv())

    reputation = response["nodes"][0]["reputation"]
    assert reputation["crashes"] == 0, "a client's mistake must not count as a node crash"
    assert reputation["timeouts"] == 0
    assert reputation["disconnects"] == 0
    assert reputation["client_faults"] == 1


async def test_client_fault_is_relayed_with_its_fault_and_exposure(tmp_path):
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
            node_task = asyncio.create_task(
                _run_fake_node(node_ws, lambda msg: dict(_CLIENT_FAULT_REPLY))
            )

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())

            node_task.cancel()

    assert response["type"] == "complete_error"
    assert response["fault"] == "client"
    assert "maximum context length" in response["reason"]
    assert response["exposed"] == [
        {
            "node_handle": crypto.handle(b"s" * 32, public_key),
            "identity_handle": response["exposed"][0]["identity_handle"],
        }
    ], "vLLM read the conversation before rejecting it, so the node saw it"
    assert isinstance(response["elapsed_ms"], int)


async def test_client_fault_does_not_fail_over_to_another_node(tmp_path):
    """A bad request would fail identically on a second volunteer, so
    retrying only spreads the client's mistake across more nodes."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    routed_to: list[str] = []

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_a_ws:
            payload_a, _ = _register_payload("node-a", "m")
            await node_a_ws.send(json.dumps(payload_a))
            await node_a_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b_ws:
                payload_b, _ = _register_payload("node-b", "m")
                await node_b_ws.send(json.dumps(payload_b))
                await node_b_ws.recv()

                def record(node_id):
                    def reply(msg):
                        routed_to.append(node_id)
                        return dict(_CLIENT_FAULT_REPLY)
                    return reply

                task_a = asyncio.create_task(_run_fake_node(node_a_ws, record("node-a")))
                task_b = asyncio.create_task(_run_fake_node(node_b_ws, record("node-b")))

                async with websockets.connect(
                    f"wss://127.0.0.1:{port}", ssl=client_ctx
                ) as client_ws:
                    await client_ws.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                    ))
                    response = json.loads(await client_ws.recv())

                task_a.cancel()
                task_b.cancel()

    assert response["fault"] == "client"
    assert len(routed_to) == 1, f"request was routed to {routed_to} — must not fail over"


async def test_coordinator_side_validation_rejection_carries_a_client_fault(tmp_path):
    """A malformed request is the client's mistake whether vLLM or the
    coordinator caught it — an agent shouldn't need two code paths."""
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
            await client_ws.send(json.dumps({
                "type": "complete", "token": "secret-token", "model": "m",
                "prompt": "hi", "messages": [{"role": "user", "content": "hi"}],
            }))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert response["fault"] == "client"
    assert response["exposed"] == []


async def test_no_healthy_node_carries_no_fault(tmp_path):
    """Neither the client's mistake nor any node's — so no fault field."""
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
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "nope", "prompt": "hi"}
            ))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert "fault" not in response
```

Finally, extend the existing `test_complete_request_node_failure_increments_crash_counter` with one line so the two paths are pinned apart — its fake node replies with no `fault`, which must still record a crash:

```python
                assert response["nodes"][0]["reputation"]["crashes"] == 1
                assert response["nodes"][0]["reputation"]["client_faults"] == 0
```

`crypto` is already imported in this test file (it is used for fingerprint assertions); confirm before relying on it.

- [ ] **Step 4: Implement the coordinator**

In `_handle_complete_request`, give `reject` an optional fault:

```python
    async def reject(reason: str, fault: str | None = None) -> None:
        reply = {"type": "complete_error", "reason": reason, "exposed": exposed}
        if fault is not None:
            reply["fault"] = fault
        if attempts:
            reply["elapsed_ms"] = int((time.monotonic() - started) * 1000)
```

Pass `fault="client"` from the two coordinator-side validation rejections (the missing-`model` guard and the `InvalidCompletionRequest` handler) — a malformed request is the client's mistake whether vLLM or the coordinator caught it. Leave the `RoutingError` branch's `reject(str(exc))` without a fault: `no healthy node` belongs to neither party.

Add the new branch **after** `except router.NodeError` and **before** `except router.RoutingError`:

```python
        except router.ClientRequestError as exc:
            # The node received the conversation and vLLM read it before
            # rejecting it, so this is exposure like any other outcome
            # (issue #63). But the fault is the client's: no reputation
            # damage, and no failover — a bad request fails identically
            # on a second volunteer. See the design doc for issue #58.
            exposed.append(registry.handles_for(node.public_key))
            registry.record_client_fault(node.public_key)
            await reject(str(exc), fault="client")
            return
```

Extend the function's docstring to record that client-caused faults are now separated from node-caused ones, citing issue #58.

- [ ] **Step 5: Run the whole suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/mycelium/coordinator/router.py src/mycelium/coordinator/server.py \
        tests/coordinator/test_router.py tests/coordinator/test_server.py
git commit -m "fix: a client's own mistake no longer damages node reputation (#58)

ClientRequestError is a RoutingError and deliberately not a NodeError, so
no existing handler can record a crash for it. The hop still counts as
exposure — the node read the conversation before vLLM rejected it — and
the client is told whose fault it was, which is what lets an agent decide
between trimming its context and retrying elsewhere.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Glossary

**Files:**
- Modify: `CONTEXT.md`

- [ ] **Step 1: Add the term**

`CONTEXT.md`'s Phase 2 section already defines **Handle** after **Exposure**. Add **Fault** after **Handle**, matching the file's format exactly:

```markdown
**Fault**:
Whose mistake a failure was: the client's (a malformed request, context
exceeding the model's window) or the node's (a crash, a timeout, a model it
isn't serving). Deliberately not the same as *who failed* — a node reports a
client fault for a request it served correctly and the model refused. Never a
judgement about the quality of a completion, which nothing in Mycelium
measures.
_Avoid_: error, blame, cause
```

- [ ] **Step 2: Verify and commit**

Run: `grep -n -A8 "^\*\*Handle\*\*" CONTEXT.md`
Confirm the new entry sits among the Phase 2 terms and matches their formatting.

```bash
git add CONTEXT.md
git commit -m "docs: define 'fault' in the glossary (#58)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin phase-2/issue-58-fault-classification
```

Open the PR against `main`, titled `Client-caused faults must not damage node reputation (#58)`. The body should state: the defect and why Phase 2 makes it easier to hit; that 404, 408 and 429 are node faults despite being 4xx, with reasons; that the node self-reports and is untrusted, hence the observable-but-non-routing counter; that a client fault still counts as exposure; and that anything not exactly `"client"` is a node fault.

---

## Acceptance criteria mapping

| #58 criterion | Where it is met |
|---|---|
| A request rejected as too long leaves reputation unchanged | Task 4 Step 3 (`..._leaves_reputation_untouched`) |
| A genuine node fault still records against reputation | Task 4 Step 3 (extended crash-counter test) |
| Context overflow returns a specific, human-readable reason | Task 1 Step 2 (`..._with_vllm_message`, asserting the limit and requested size survive) |
| The node never truncates a caller's context | Unchanged — no truncation logic exists or is added; `complete()` still sends the array as-is |
| Reputation-weighted routing otherwise unchanged | Task 3 Step 2 (`..._do_not_affect_routing_weight`), plus the whole suite staying green |

## Notes for whoever picks this up

- The `client_faults` counter must never reach `_reputation_weight()`. If a test seems to want it there, the test is wrong.
- Do not attempt to verify a node's fault claim. The coordinator never sees vLLM's request or response; making the claim checkable is explicitly out of scope and would need a different trust model.
- `urllib.error.HTTPError` subclasses `URLError`. Catching `URLError` instead would silently swallow transport failures into the classification path.
- #59 consumes `fault` from the client-facing reply. Do not add client-side handling here.
