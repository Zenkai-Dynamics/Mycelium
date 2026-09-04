# Carry a messages array end to end — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A client can send a multi-turn `messages` array that reaches vLLM with system/user/assistant roles intact, while the existing single-`prompt` form keeps working as a one-message shorthand.

**Architecture:** The coordinator becomes the single normalization point. Its client-facing wire accepts `prompt` **xor** `messages`; it validates structurally, expands the shorthand, and forwards only `messages` to the node. The node therefore deletes its own prompt-wrapping and passes the array straight to vLLM's chat endpoint — which is the sense in which this makes the node simpler rather than harder.

**Tech Stack:** Python 3.11+, `websockets` 17.0.1, `pytest` 9.1.1 with `pytest-asyncio` in `asyncio_mode = "auto"`, stdlib `urllib` for the vLLM call.

## Global Constraints

- Issue: [#55](https://github.com/Zenkai-Dynamics/Mycelium/issues/55). Parent: [#54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54).
- Design authority: `docs/superpowers/specs/2026-09-04-phase-2-implementation-design.md`. Where this plan and that document disagree, the document wins.
- Branch: `phase-2/issue-55-messages-array`, off `main`. One PR at the end.
- No test may make a real network call to GitHub or start a real vLLM. Fakes only, matching the existing suite.
- `role` is **not** restricted to a vocabulary. Any non-empty string role passes validation — chat templates already use `tool`, and Mycelium must not be the component that breaks a legitimate request.
- The node never truncates a caller's context to make it fit.
- Style: match the surrounding code. This codebase writes long explanatory docstrings that record *why*, and comments that cite the issue whose design doc explains a decision. Follow that.
- Run the whole suite (`pytest`) before every commit, not just the new tests. Every commit must leave it green.

---

### Task 1: The node wire carries `messages`

Everything from `route_request` down speaks `messages`. The coordinator still accepts only `prompt` from clients at this point and wraps it inline, so the system stays working end to end and the suite stays green. Task 2 opens the client-facing side.

This task is deliberately not split further: changing `vllm_process` alone leaves `request_handler` calling it wrongly, and changing the node alone leaves the coordinator sending a field the node no longer reads. The smallest change that keeps the suite green covers all four files.

**Files:**
- Modify: `src/mycelium/node/vllm_process.py:126-138` (`complete`)
- Modify: `src/mycelium/node/request_handler.py:46-66` (`_handle_complete`)
- Modify: `src/mycelium/coordinator/router.py:63-99` (`route_request`)
- Modify: `src/mycelium/coordinator/server.py:129-200` (`_handle_complete_request`)
- Test: `tests/node/test_vllm_process.py`, `tests/node/test_request_handler.py`, `tests/coordinator/test_router.py`, `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `vllm_process.VLLMProcess.complete(messages: list[dict], timeout: float = COMPLETE_TIMEOUT_SECONDS) -> str`; `router.route_request(node: Node, messages: list[dict], timeout: float = NODE_COMPLETE_TIMEOUT_SECONDS) -> str`; node-facing wire message `{"type": "complete", "request_id": str, "messages": list[dict]}`.

- [ ] **Step 1: Write the failing test for `complete(messages)`**

In `tests/node/test_vllm_process.py`, add this alongside the existing `fake_vllm_server` tests. It needs the fake handler to record what it received, so first add a module-level capture list and record into it — replace the existing `self.rfile.read(length)` line in `_FakeVLLMHandler.do_POST` with a version that keeps the body:

```python
RECEIVED_BODIES: list[dict] = []


# inside _FakeVLLMHandler.do_POST, replacing `self.rfile.read(length)`:
            RECEIVED_BODIES.append(json.loads(self.rfile.read(length)))
```

Then the test:

```python
def test_complete_sends_the_messages_array_through_unchanged(fake_vllm_server):
    RECEIVED_BODIES.clear()
    port = fake_vllm_server.server_address[1]
    process = VLLMProcess(model="test-model", port=port)

    messages = [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "and of Spain?"},
    ]
    text = process.complete(messages)

    assert text == "the answer is 42"
    assert RECEIVED_BODIES[0]["messages"] == messages, (
        "roles must survive to vLLM intact, not be flattened into one user message"
    )
    assert RECEIVED_BODIES[0]["model"] == "test-model"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `pytest tests/node/test_vllm_process.py::test_complete_sends_the_messages_array_through_unchanged -v`
Expected: FAIL — the current `complete` wraps its argument as `{"role": "user", "content": <the list>}`, so the assertion on `["messages"]` does not match.

- [ ] **Step 3: Make `complete` take `messages`**

In `src/mycelium/node/vllm_process.py`, replace the `complete` method:

```python
    def complete(self, messages: list[dict], timeout: float = COMPLETE_TIMEOUT_SECONDS) -> str:
        """Forward a conversation to vLLM's OpenAI-compatible chat endpoint,
        return the completion text.

        Takes the `messages` array as-is rather than a bare prompt string:
        as of issue #55 the coordinator is the single place that expands
        the one-message `prompt` shorthand, so by the time a request
        reaches a node it always carries real roles. This method no longer
        constructs any part of the conversation itself.
        """
        url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        payload = json.dumps({"model": self.model, "messages": messages}).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return body["choices"][0]["message"]["content"]
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `pytest tests/node/test_vllm_process.py -v`
Expected: PASS. Any other test in this file that called `complete("some string")` now fails — update those call sites to pass `[{"role": "user", "content": "some string"}]`.

- [ ] **Step 5: Update the request handler's tests to the new wire**

In `tests/node/test_request_handler.py`, `_FakeProcess.complete` records a prompt string. Change it to record message arrays:

```python
class _FakeProcess:
    """Stand-in for VLLMProcess — records the messages array it was called
    with and either returns a canned completion or raises."""

    def __init__(self, result=None, error=None):
        self.calls: list[list[dict]] = []
        self._result = result
        self._error = error

    def complete(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        return self._result
```

Then update the two tests that send a `prompt` — `test_handle_messages_replies_with_completion_on_success` and `test_handle_messages_replies_with_error_when_complete_raises` — to send `"messages"` instead, and add one asserting the array reaches the process untouched:

```python
async def test_handle_messages_forwards_the_messages_array_to_the_process():
    process = _FakeProcess(result="the answer")
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "what's up?"},
    ]
    websocket = _FakeWebsocket([
        json.dumps({"type": "complete", "request_id": "abc", "messages": messages})
    ])

    handler_task = asyncio.create_task(handle_messages(websocket, process))
    await asyncio.sleep(0.05)  # let the spawned per-message task finish and reply
    websocket.close_from_test()
    await handler_task

    assert process.calls == [messages]
    reply = json.loads(websocket.sent[0])
    assert reply == {"type": "complete_result", "request_id": "abc", "text": "the answer"}
```

In `test_handle_messages_replies_with_completion_on_success`, change the incoming message's `"prompt": "what's up?"` to `"messages": [{"role": "user", "content": "what's up?"}]` and its assertion `process.calls == ["what's up?"]` to `process.calls == [[{"role": "user", "content": "what's up?"}]]`. Make the same `prompt` → `messages` substitution in `test_handle_messages_replies_with_error_when_complete_raises` and in `test_handle_messages_handles_multiple_requests_concurrently`.

- [ ] **Step 6: Run and confirm they fail**

Run: `pytest tests/node/test_request_handler.py -v`
Expected: FAIL — `_handle_complete` still reads `message.get("prompt")`, so `_FakeProcess.complete` receives `None`.

- [ ] **Step 7: Make the handler read `messages`**

In `src/mycelium/node/request_handler.py`, inside `_handle_complete`, replace the `prompt` lookup and the `to_thread` call:

```python
    request_id = message.get("request_id")
    messages = message.get("messages")
    try:
        # Broad except is deliberate here, not sloppy: whatever goes wrong
        # calling vLLM (HTTP error, timeout, malformed response) becomes a
        # complete_error the coordinator/client can see, per the design
        # doc for issue #10 — never left to hang or crash this task.
        text = await asyncio.to_thread(process.complete, messages)
```

- [ ] **Step 8: Run and confirm they pass**

Run: `pytest tests/node/ -v`
Expected: PASS.

- [ ] **Step 9: Write the failing router test**

`tests/coordinator/test_router.py` already defines `_make_node()`, `_FakeNodeWebsocket` and `_resolve_after(node, delay, message)`. Use them — do not add a second fake.

Every existing test in that file passes a bare string as the second argument to `route_request`. Change each of those call sites to a messages array, e.g. `router.route_request(node, [{"role": "user", "content": "what's up?"}], timeout=2.0)`, and in `test_route_request_sends_a_complete_message_with_request_id` replace the `assert sent["prompt"] == "what's up?"` line with the array assertion below. Then add:

```python
async def test_route_request_sends_the_messages_array_to_the_node():
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    node = _make_node()
    asyncio.create_task(_resolve_after(node, 0.05, {"type": "complete_result", "text": "ok"}))

    text = await router.route_request(node, messages, timeout=2.0)

    sent = json.loads(node.websocket.sent[0])
    assert sent["messages"] == messages
    assert "prompt" not in sent, "the node wire carries messages only, as of issue #55"
    assert text == "ok"
```

- [ ] **Step 10: Run and confirm it fails**

Run: `pytest tests/coordinator/test_router.py -v`
Expected: FAIL — `route_request` still sends `"prompt"`.

- [ ] **Step 11: Change `route_request`**

In `src/mycelium/coordinator/router.py`, change the signature and the frame it sends:

```python
async def route_request(
    node: Node, messages: list[dict], timeout: float = NODE_COMPLETE_TIMEOUT_SECONDS
) -> str:
    """Send `messages` to `node` over its already-open connection and
    return its completion text.

    Carries the conversation as an array of {role, content} rather than a
    bare prompt string — see the design doc for issue #55. The coordinator
    has already expanded any `prompt` shorthand by the time this is
    called, so the node wire has exactly one shape.

    Raises NodeDisconnectedError if the connection is or becomes unusable,
    NodeTimeoutError if no reply arrives within `timeout`, or NodeError if
    the node explicitly reports a failure. `node.pending` never retains an
    entry for this request once this function returns or raises.
    """
```

and inside it, the send:

```python
            await node.websocket.send(
                json.dumps(
                    {"type": "complete", "request_id": request_id, "messages": messages}
                )
            )
```

- [ ] **Step 12: Update the coordinator's call site and its fake nodes**

In `src/mycelium/coordinator/server.py`, inside `_handle_complete_request`, wrap the prompt at the call site for now — Task 2 replaces this with real normalization:

```python
            text = await router.route_request(
                node,
                [{"role": "user", "content": prompt}],
                timeout=router.NODE_COMPLETE_TIMEOUT_SECONDS,
            )
```

In `tests/coordinator/test_server.py`, every fake node built with `_run_fake_node(node_ws, lambda msg: ...)` that reads `msg['prompt']` must now read the array. Search for `msg['prompt']` and `msg["prompt"]` and change each to use the last message's content, e.g.:

```python
                lambda msg: {"type": "complete_result", "text": f"echo: {msg['messages'][-1]['content']}"}
```

- [ ] **Step 13: Run the whole suite**

Run: `pytest`
Expected: PASS. A client sending `prompt` still works end to end; the node now receives roles.

- [ ] **Step 14: Commit**

```bash
git add src/mycelium/node/vllm_process.py src/mycelium/node/request_handler.py \
        src/mycelium/coordinator/router.py src/mycelium/coordinator/server.py \
        tests/node/test_vllm_process.py tests/node/test_request_handler.py \
        tests/coordinator/test_router.py tests/coordinator/test_server.py
git commit -m "refactor: node wire carries a messages array (#55)

The node no longer builds any part of the conversation: route_request
sends {role, content} entries and vllm_process forwards them to vLLM
unchanged. The coordinator still only accepts a prompt from clients and
wraps it at the call site — the client-facing side opens next.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Client-facing `prompt` xor `messages`, with validation

**Files:**
- Create: `src/mycelium/coordinator/completion_request.py`
- Create: `tests/coordinator/test_completion_request.py`
- Modify: `src/mycelium/coordinator/server.py:129-200` (`_handle_complete_request`)
- Test: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `router.route_request(node, messages, timeout)` from Task 1.
- Produces: `completion_request.normalize_messages(message: dict) -> list[dict]`, raising `completion_request.InvalidCompletionRequest` with a client-facing reason string.

A separate module rather than another private function in `server.py`: this is pure, it is the one piece of Phase 2 wire policy worth testing without standing up a websocket server, and `server.py` is already 429 lines.

- [ ] **Step 1: Write the failing tests for the pure function**

Create `tests/coordinator/test_completion_request.py`:

```python
"""Tests for mycelium.coordinator.completion_request."""

import pytest

from mycelium.coordinator.completion_request import (
    InvalidCompletionRequest,
    normalize_messages,
)


def test_prompt_becomes_a_single_user_message():
    assert normalize_messages({"prompt": "hello"}) == [{"role": "user", "content": "hello"}]


def test_messages_pass_through_unchanged():
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert normalize_messages({"messages": messages}) == messages


def test_both_prompt_and_messages_is_rejected_as_ambiguous():
    with pytest.raises(InvalidCompletionRequest) as exc:
        normalize_messages({"prompt": "hi", "messages": [{"role": "user", "content": "hi"}]})
    assert "both" in str(exc.value).lower()


def test_neither_prompt_nor_messages_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({})


def test_empty_messages_list_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"messages": []})


def test_empty_prompt_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"prompt": ""})


def test_message_that_is_not_an_object_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"messages": ["just a string"]})


def test_message_missing_content_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"messages": [{"role": "user"}]})


def test_message_with_non_string_content_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"messages": [{"role": "user", "content": 42}]})


def test_unfamiliar_role_is_accepted():
    """Role is not restricted to a vocabulary — chat templates already use
    'tool', and rejecting it would make Mycelium the component breaking a
    legitimate request. See the Phase 2 implementation design doc."""
    messages = [{"role": "tool", "content": "42"}]
    assert normalize_messages({"messages": messages}) == messages
```

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/coordinator/test_completion_request.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.coordinator.completion_request'`.

- [ ] **Step 3: Write the module**

Create `src/mycelium/coordinator/completion_request.py`:

```python
"""Resolves a client's completion request into the messages array the
node wire carries.

See the design doc for issue #55, and the Phase 2 implementation design
doc for why the coordinator is the single normalization point: the node
then has exactly one shape to handle and builds no part of the
conversation itself.

Validation here is deliberately structural only — a non-empty list of
objects with a string role and string content. `role` is NOT checked
against a vocabulary: chat templates already use roles beyond
system/user/assistant ("tool" being the obvious one), and rejecting
those would make Mycelium the component that breaks a legitimate
request. Anything vLLM itself rejects comes back as a client-caused
fault (issue #58) rather than being pre-empted here.
"""

from __future__ import annotations


class InvalidCompletionRequest(Exception):
    """Raised when a completion request names neither prompt nor
    messages, names both, or carries a malformed messages array. The
    message text is client-facing — it is relayed verbatim as a
    complete_error reason."""


def normalize_messages(message: dict) -> list[dict]:
    """Return the messages array for `message`, expanding the one-message
    `prompt` shorthand if that is what was sent.

    Raises InvalidCompletionRequest, whose text is safe to relay to the
    client, if the request is ambiguous or malformed.
    """
    prompt = message.get("prompt")
    messages = message.get("messages")

    if prompt is not None and messages is not None:
        # Deliberately not resolved by a precedence rule — a caller who
        # sets both is confused and should be told, rather than having
        # one silently dropped. See the design doc for issue #55.
        raise InvalidCompletionRequest(
            "send either prompt or messages, not both"
        )
    if prompt is None and messages is None:
        raise InvalidCompletionRequest("either prompt or messages is required")

    if prompt is not None:
        if not isinstance(prompt, str) or not prompt:
            raise InvalidCompletionRequest("prompt must be a non-empty string")
        return [{"role": "user", "content": prompt}]

    if not isinstance(messages, list) or not messages:
        raise InvalidCompletionRequest("messages must be a non-empty list")
    for index, entry in enumerate(messages):
        if not isinstance(entry, dict):
            raise InvalidCompletionRequest(
                f"messages[{index}] must be an object with role and content"
            )
        role = entry.get("role")
        content = entry.get("content")
        if not isinstance(role, str) or not role:
            raise InvalidCompletionRequest(
                f"messages[{index}] needs a non-empty string role"
            )
        if not isinstance(content, str):
            raise InvalidCompletionRequest(
                f"messages[{index}] needs a string content"
            )
    return messages
```

- [ ] **Step 4: Run and confirm they pass**

Run: `pytest tests/coordinator/test_completion_request.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Write the failing server tests**

In `tests/coordinator/test_server.py`, add three tests. The first needs a registered fake node; copy the structure of the existing `test_complete_request_node_reports_failure_is_relayed_to_client` for that. The other two need no node at all, since the rejection happens before routing — copy `test_complete_request_with_missing_prompt_returns_error`.

```python
async def test_complete_request_with_messages_reaches_the_node_intact(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "and of Spain?"},
    ]
    received: list[dict] = []

    def reply(msg):
        received.append(msg)
        return {"type": "complete_result", "text": "Madrid."}

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, _ = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()  # consume "registered"
            node_task = asyncio.create_task(_run_fake_node(node_ws, reply))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps({
                    "type": "complete", "token": "secret-token",
                    "model": "m", "messages": messages,
                }))
                response = json.loads(await client_ws.recv())

            node_task.cancel()

    assert response["type"] == "complete_result"
    assert response["text"] == "Madrid."
    assert received[0]["messages"] == messages, (
        "roles must survive coordinator -> node with the array unchanged"
    )


async def test_complete_request_with_both_prompt_and_messages_is_rejected(tmp_path):
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
    assert "both" in response["reason"].lower()


async def test_complete_request_with_malformed_messages_is_rejected(tmp_path):
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
                "messages": [{"role": "user"}],
            }))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert "content" in response["reason"]
```

- [ ] **Step 6: Run and confirm they fail**

Run: `pytest tests/coordinator/test_server.py -k "messages" -v`
Expected: FAIL — the server still requires `prompt` and rejects a request without one.

- [ ] **Step 7: Wire the coordinator up to the module**

In `src/mycelium/coordinator/server.py`, add the import next to the existing coordinator imports:

```python
from mycelium.coordinator import completion_request, github_identity, router
```

Then in `_handle_complete_request`, replace the `model`/`prompt` guard with a model guard plus normalization, and use the result in the `route_request` call. The `reject` helper is defined a few lines below the current guard — move its definition above this block so the new code can use it:

```python
    async def reject(reason: str) -> None:
        try:
            await websocket.send(json.dumps({"type": "complete_error", "reason": reason}))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.close()

    model = message.get("model")
    if not model:
        await reject("model is required")
        return

    # The coordinator is the single place the one-message `prompt`
    # shorthand is expanded, so the node wire has exactly one shape —
    # see the design doc for issue #55.
    try:
        messages = completion_request.normalize_messages(message)
    except completion_request.InvalidCompletionRequest as exc:
        await reject(str(exc))
        return
```

and the routing call becomes:

```python
            text = await router.route_request(
                node, messages, timeout=router.NODE_COMPLETE_TIMEOUT_SECONDS
            )
```

Delete the now-duplicated `reject` definition that followed the old guard, and the old `prompt = message.get("prompt")` line.

- [ ] **Step 8: Run the whole suite**

Run: `pytest`
Expected: PASS. `test_complete_request_with_missing_prompt_returns_error` still passes — a request with neither field is still an error, now with a clearer reason.

- [ ] **Step 9: Commit**

```bash
git add src/mycelium/coordinator/completion_request.py \
        src/mycelium/coordinator/server.py \
        tests/coordinator/test_completion_request.py \
        tests/coordinator/test_server.py
git commit -m "feat: accept a messages array from clients (#55)

prompt and messages are mutually exclusive: sending both is rejected as
ambiguous rather than resolved by a silent precedence rule, and sending
neither is rejected as before. Validation is structural only — role is
not checked against a vocabulary, since chat templates already use
roles beyond system/user/assistant.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Client library and `--messages-file`

**Files:**
- Modify: `src/mycelium/client/cli.py`
- Test: `tests/client/test_cli.py`

**Interfaces:**
- Consumes: the client-facing wire from Task 2.
- Produces: `client.cli.complete(coordinator_url, coordinator_cert, token, model, prompt=None, messages=None, timeout=CLIENT_COMPLETE_TIMEOUT_SECONDS) -> str`; CLI flag `--messages-file PATH`, mutually exclusive with `--prompt`.

`Flow` (issue #59) does not use this function — it gets its own transport module. This keeps the shipped CLI able to exercise multi-turn before the library exists, which is what #55's acceptance criteria and #61's live session both need.

- [ ] **Step 1: Write the failing tests**

`tests/client/test_cli.py` has no shared fixture — each test stands up a real `server.serve(...)` and an inline `fake_node()` coroutine. Follow that exact shape; do not introduce a fixture.

Note the existing `fake_node()` bodies interpolate `msg['prompt']`, which no longer exists after Task 1. Change those to `msg['messages'][-1]['content']` while you are in the file — including the `assert text == "echo: hello"` that depends on it.

The tests below use `Path`, which this file does not currently import. Add `from pathlib import Path` to its imports.

```python
async def test_complete_sends_a_messages_array_when_given_one(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    received: list[dict] = []

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            private_key = crypto.generate_keypair()
            await node_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": crypto.public_key_b64(private_key),
                "signature": crypto.sign_public_key(private_key),
                "github_token": "valid-github-token",
            }))
            await node_ws.recv()

            async def fake_node():
                msg = json.loads(await node_ws.recv())
                received.append(msg)
                await node_ws.send(json.dumps({
                    "type": "complete_result",
                    "request_id": msg["request_id"],
                    "text": "the answer",
                }))

            node_task = asyncio.create_task(fake_node())

            text = await complete(
                f"wss://127.0.0.1:{port}", cert_path, "secret-token", "m", messages=messages
            )
            await node_task

    assert text == "the answer"
    assert received[0]["messages"] == messages
    assert "prompt" not in received[0]


async def test_complete_rejects_both_prompt_and_messages_before_connecting():
    """No coordinator is started at all: an ambiguous request must fail
    locally, so an unreachable URL is never dialed."""
    with pytest.raises(CompletionError) as exc:
        await complete(
            "wss://127.0.0.1:1", Path("nonexistent.pem"), "secret-token", "m",
            prompt="hi", messages=[{"role": "user", "content": "hi"}],
        )
    assert "both" in str(exc.value).lower()


async def test_complete_rejects_neither_prompt_nor_messages():
    with pytest.raises(CompletionError):
        await complete("wss://127.0.0.1:1", Path("nonexistent.pem"), "secret-token", "m")


def test_parse_args_rejects_prompt_and_messages_file_together(tmp_path):
    messages_file = tmp_path / "conv.json"
    messages_file.write_text("[]")
    with pytest.raises(SystemExit):
        cli.parse_args([
            "--coordinator-url", "wss://x", "--coordinator-cert", "c.pem",
            "--token-file", "t.txt", "--model", "m",
            "--prompt", "hi", "--messages-file", str(messages_file),
        ])


def test_parse_args_requires_one_of_prompt_or_messages_file():
    with pytest.raises(SystemExit):
        cli.parse_args([
            "--coordinator-url", "wss://x", "--coordinator-cert", "c.pem",
            "--token-file", "t.txt", "--model", "m",
        ])
```

If the existing fixture does not already record what it received or expose a `canned_text`, extend it to do so rather than inlining a new fake.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/client/test_cli.py -v`
Expected: FAIL — `complete()` takes a positional `prompt` and has no `messages` parameter; `parse_args` has a required `--prompt`.

- [ ] **Step 3: Update `complete` and `parse_args`**

In `src/mycelium/client/cli.py`:

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-client")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    # Mutually exclusive and required: the wire rejects both-set as
    # ambiguous (issue #55), so the CLI refuses to build such a request
    # in the first place rather than making the user learn that by
    # round-tripping.
    content = parser.add_mutually_exclusive_group(required=True)
    content.add_argument("--prompt")
    content.add_argument(
        "--messages-file",
        type=Path,
        help="path to a JSON file holding a [{\"role\": ..., \"content\": ...}, ...] array",
    )
    return parser.parse_args(argv)


async def complete(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    model: str,
    prompt: str | None = None,
    messages: list[dict] | None = None,
    timeout: float = CLIENT_COMPLETE_TIMEOUT_SECONDS,
) -> str:
    """Send one prompt or conversation to the coordinator and return the
    completion text.

    Exactly one of `prompt` or `messages` must be given — the same rule the
    wire enforces (see the design doc for issue #55), checked here so an
    ambiguous request fails locally instead of after a round trip.

    Raises CompletionError if the request is ambiguous, the coordinator
    rejects it, the routed node fails or is unavailable, or no response
    arrives in time.
    """
    if (prompt is None) == (messages is None):
        raise CompletionError("send either prompt or messages, not both or neither")

    request = {"type": "complete", "token": token, "model": model}
    if prompt is not None:
        request["prompt"] = prompt
    else:
        request["messages"] = messages

    ssl_context = build_ssl_context(coordinator_cert)
    async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
        await websocket.send(json.dumps(request))
```

The rest of `complete`'s body — the timeout, the `recv`, and the response dispatch — is unchanged.

- [ ] **Step 4: Update `main` to read the file**

```python
def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    messages = None
    if args.messages_file is not None:
        try:
            messages = json.loads(args.messages_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read {args.messages_file}: {exc}", flush=True)
            sys.exit(1)
    try:
        text = asyncio.run(
            complete(
                args.coordinator_url, args.coordinator_cert, token, args.model,
                prompt=args.prompt, messages=messages,
            )
        )
    except CompletionError as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    print(text, flush=True)
```

Note the coordinator, not the client, validates the array's structure — a malformed array comes back as a `complete_error` with a specific reason. The client only ensures the file is readable JSON.

- [ ] **Step 5: Run the whole suite**

Run: `pytest`
Expected: PASS. Existing tests calling `complete(url, cert, token, model, "some prompt")` positionally still work, since `prompt` remains the fifth positional parameter.

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/client/cli.py tests/client/test_cli.py
git commit -m "feat: mycelium-client --messages-file for multi-turn requests (#55)

A JSON file rather than repeatable role:content flags, because message
content routinely contains colons, quotes and newlines. Mutually
exclusive with --prompt, matching the wire's rejection of both.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Document the new form

**Files:**
- Modify: `docs/OPERATIONS.md`

- [ ] **Step 1: Read the existing client section**

Run: `grep -n "mycelium-client" docs/OPERATIONS.md`

Read the surrounding section in full. Match its voice and its level of detail — it is a working operations guide with real commands, not reference documentation.

- [ ] **Step 2: Extend it**

Immediately after the existing single-prompt client example, add a multi-turn subsection. Keep the existing example intact and unchanged — it is still the shorthand and still correct:

````markdown
### Sending a multi-turn conversation

`--prompt` is shorthand for a single user message. To send a conversation
with roles intact — a system instruction, prior turns, or a previous
model's output — write the array to a file and pass `--messages-file`:

```json
[
  {"role": "system", "content": "You answer in one sentence."},
  {"role": "user", "content": "What is the capital of France?"},
  {"role": "assistant", "content": "Paris."},
  {"role": "user", "content": "And of Spain?"}
]
```

```bash
mycelium-client \
  --coordinator-url wss://coordinator.example:8765 \
  --coordinator-cert coordinator.pem \
  --token-file client-token.txt \
  --model Qwen/Qwen2.5-7B-Instruct \
  --messages-file conversation.json
```

The roles reach the model intact rather than being flattened into one
blob, which is what chat-tuned models are trained to expect.

`--prompt` and `--messages-file` are mutually exclusive. Sending both is
rejected rather than resolved by a precedence rule: a request that sets
both is ambiguous, and silently dropping one of them is exactly the bug
that is painful to trace back from a wrong answer.

`role` is not restricted to `system`/`user`/`assistant` — any non-empty
string is passed through, since chat templates use others. Each entry
must be an object with a string `role` and a string `content`; anything
else comes back as a `complete_error` naming the offending index.

Keeping the conversation within the model's context window is the
caller's job. The node never silently truncates.
````

- [ ] **Step 3: Verify the commands you documented actually parse**

Run: `python -c "from mycelium.client.cli import parse_args; print(parse_args(['--coordinator-url','wss://x','--coordinator-cert','c.pem','--token-file','t.txt','--model','m','--messages-file','conv.json']))"`
Expected: prints a Namespace with `messages_file=PosixPath('conv.json')` and `prompt=None`.

- [ ] **Step 4: Commit and open the PR**

```bash
git add docs/OPERATIONS.md
git commit -m "docs: document --messages-file and the prompt/messages rule (#55)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin phase-2/issue-55-messages-array
```

Open the PR against `main`, titled `Carry a messages array end to end (#55)`, with a body that states: the coordinator is now the single normalization point; the node builds no part of the conversation; both-set is rejected as ambiguous; role is deliberately unrestricted; and that nothing about the existing `--prompt` path changed.

---

## Acceptance criteria mapping

| #55 criterion | Where it is met |
|---|---|
| Multi-turn array returns a contextually-correct completion | Task 2 Step 5 (`..._reaches_the_node_intact`), live-confirmed in #61 |
| Roles survive to vLLM, not collapsed | Task 1 Step 1, Task 2 Step 5 |
| Existing single-`prompt` form unchanged | Task 2 Step 8 — the whole existing suite stays green |
| Both fields rejected with a clear error | Task 2 Steps 1 and 5 |
| Neither field rejected with a clear error | Task 2 Step 1 (`test_neither_prompt_nor_messages_is_rejected`) |
| `docs/OPERATIONS.md` documents the new form | Task 4 |

## Notes for whoever picks this up

- The suite asserts exact dict equality on replies in several places, e.g. `assert response == {"type": "complete_result", "text": "echo: hello"}`. That is fine for this issue, which adds no reply fields — but issue #63 adds `exposed` and `elapsed_ms` to those same replies and will have to loosen them. Do not loosen them here in anticipation.
- Do not add `fault` handling to `complete_error` here. That is #58, and the two issues are independent — either may land first.
