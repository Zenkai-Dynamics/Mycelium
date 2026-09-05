# Multi-Hop Client Library — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `Flow` that carries one logical task across several models, keeping a complete local record of every hop — while sending only what each call explicitly names.

**Architecture:** A new `client/transport.py` owns one connect-send-receive-close round trip and nothing else; both the existing CLI and the new `Flow` use it. `client/flow.py` holds `Flow`, the frozen `Hop` and `Exposure` records, `HopError`, and the three message helpers. There is deliberately no implicit accumulator: each call states its own content, and the record is kept locally and never transmitted.

**Tech Stack:** Python 3.11+, `websockets` 17.0.1, stdlib `dataclasses`/`copy`/`time`, `pytest` 9.1.1 with `pytest-asyncio` in `asyncio_mode = "auto"`.

## Global Constraints

- Issue: [#59](https://github.com/Zenkai-Dynamics/Mycelium/issues/59). Parent: [#54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54). Blocks #60.
- Design authority: `docs/superpowers/specs/2026-09-05-issue-59-flow-library-design.md`, and behind it `docs/superpowers/specs/2026-09-04-phase-2-implementation-design.md` and [ADR-0004](../../adr/0004-explicit-per-hop-context.md). Where this plan and those disagree, the documents win.
- Branch: `phase-2/issue-59-flow-library`, off `main`. One PR at the end.
- **There is no implicit accumulator.** `Flow` must have no code path that sends content a call did not name. This is the property the whole library exists for.
- `call` **deep-copies** what it sends. The record is the evidence behind the exposure report; a reference the caller can mutate is not evidence.
- **No client-side validation of `messages`.** The coordinator owns that rule (#55). Flow requires `messages` at the API level only.
- No `list_models()` — the wire request does not exist until #56.
- A null `identity_handle` is kept under a `None` key in the exposure report, never dropped.
- No coordinator or node changes. No changes to the operator CLIs (`status_cli`, `ban_cli`).
- No test may make a real network call to GitHub or start a real vLLM. Fakes only.
- Style: match the surrounding code — long docstrings recording *why*, comments citing the issue number whose design doc explains a decision.
- Run the whole suite (`pytest`) before every commit. Every commit leaves it green. The suite is currently at 314 passing.

---

### Task 1: `client/transport.py`

**Files:**
- Create: `src/mycelium/client/transport.py`
- Create: `tests/client/test_transport.py`

**Interfaces:**
- Produces: `transport.TransportError`; `async transport.request(coordinator_url: str, coordinator_cert: Path, message: dict, timeout: float) -> dict`.

- [ ] **Step 1: Write the failing tests**

Create `tests/client/test_transport.py`. It stands up a real coordinator the same way `tests/client/test_cli.py` does — read that file first and reuse its `_client_ssl_context` and `_fake_identity_verifier` helpers rather than writing new ones.

```python
"""Tests for mycelium.client.transport."""

import asyncio
import json
import ssl
from pathlib import Path

import pytest
import websockets

from mycelium.client import transport
from mycelium.coordinator import certs, github_identity, server


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    return github_identity.GithubIdentity(id="1", login="octocat")


async def test_request_returns_the_parsed_reply(tmp_path):
    """A status_query is used deliberately: transport must work for any
    request type, since it knows nothing about completions."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]

        reply = await transport.request(
            f"wss://127.0.0.1:{port}", cert_path,
            {"type": "status_query", "token": "secret-token"}, timeout=5.0,
        )

    assert reply["type"] == "status"
    assert reply["nodes"] == []


async def test_request_returns_a_complete_error_as_data(tmp_path):
    """A complete_error is a reply, not an exception — interpreting it is
    the caller's job, not transport's."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]

        reply = await transport.request(
            f"wss://127.0.0.1:{port}", cert_path,
            {"type": "complete", "token": "secret-token", "model": "nope", "prompt": "hi"},
            timeout=5.0,
        )

    assert reply["type"] == "complete_error"
    assert "no healthy node" in reply["reason"]


async def test_request_raises_when_the_coordinator_closes_without_replying(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]

        with pytest.raises(transport.TransportError, match="without responding"):
            await transport.request(
                f"wss://127.0.0.1:{port}", cert_path,
                {"type": "complete", "token": "wrong-token", "model": "m", "prompt": "hi"},
                timeout=5.0,
            )


async def test_request_raises_on_timeout(tmp_path):
    """A registered node that never answers: the coordinator holds the
    client connection open, so transport's own timeout must fire."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]

        with pytest.raises(transport.TransportError, match="did not respond"):
            await transport.request(
                f"wss://127.0.0.1:{port}", cert_path,
                {"type": "status_query", "token": "secret-token"}, timeout=0.001,
            )
```

Add one more, for the case that has no coordinator at all:

```python
async def test_request_raises_when_the_coordinator_cannot_be_reached(tmp_path):
    """A refused connection raises OSError out of websockets, not
    ConnectionClosed. Without wrapping it, a bare OSError would escape
    transport and Flow could not record the attempted hop."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    with pytest.raises(transport.TransportError, match="could not reach"):
        await transport.request(
            "wss://127.0.0.1:1", cert_path,
            {"type": "status_query", "token": "secret-token"}, timeout=5.0,
        )
```

The timeout test relies on a 1ms timeout beating a local round trip. If it proves flaky, drive it with a registered node that never replies (see `test_complete_request_timeout_increments_timeout_counter` in `tests/coordinator/test_server.py` for that shape) rather than loosening the assertion.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/client/test_transport.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.client.transport'`.

- [ ] **Step 3: Write the module**

```python
"""One request/response round trip to the coordinator.

Extracted for issue #59, when Flow became the third client-side caller of
the same connect-send-receive-close sequence — after mycelium-client and
the flow library itself. Two hand-rolled copies of this against a wire
that issues #63 and #58 both changed is the drift the extraction exists
to prevent.

Deliberately knows nothing about completions, faults or exposure: it
returns whatever reply arrives, parsed. A `complete_error` is data the
caller interprets, not an exception this module raises — the CLI turns it
into a CompletionError, Flow turns it into a HopError with the fault and
hop attached, and neither has to translate an exception it never wanted.
Only transport-level failures raise here.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import websockets

from mycelium.node.connection import build_ssl_context


class TransportError(Exception):
    """The exchange itself failed: the coordinator could not be reached, no
    reply arrived within the timeout, or the connection closed without a
    response (most often a rejected token).

    Every way the round trip can fail short of producing a reply arrives
    as this one class, so a caller has exactly one thing to catch. In
    particular a refused or unreachable connection raises OSError from
    `websockets.connect`, which would otherwise escape as a bare OSError
    and leave Flow unable to record the attempted hop.
    """


async def request(
    coordinator_url: str, coordinator_cert: Path, message: dict, timeout: float
) -> dict:
    """Send `message` to the coordinator and return its reply, parsed.

    Raises TransportError if the coordinator cannot be reached, no reply
    arrives within `timeout`, or the connection closes first. Any reply
    that does arrive is returned as-is, including a complete_error —
    deciding what a reply means belongs to the caller.
    """
    ssl_context = build_ssl_context(coordinator_cert)
    try:
        async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
            await websocket.send(json.dumps(message))
            try:
                async with asyncio.timeout(timeout):
                    raw = await websocket.recv()
            except TimeoutError:
                raise TransportError(
                    f"coordinator did not respond within {timeout}s"
                ) from None
            except websockets.exceptions.ConnectionClosed:
                raise TransportError(
                    "coordinator closed the connection without responding (check the token)"
                ) from None
    except OSError as exc:
        # Connection establishment failed — refused, unreachable host, DNS
        # failure, TLS handshake rejected. `websockets.connect` is lazy, so
        # these surface when the context manager is entered rather than
        # from the call itself. TransportError is not an OSError, so the
        # two raises above pass through this handler untouched.
        raise TransportError(f"could not reach the coordinator: {exc}") from None
    return json.loads(raw)
```

- [ ] **Step 4: Run and confirm they pass, then the whole suite**

Run: `pytest tests/client/test_transport.py -v` then `pytest`
Expected: PASS. Nothing uses the module yet.

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/client/transport.py tests/client/test_transport.py
git commit -m "feat: extract the client's coordinator round trip (#59)

Knows nothing about completions, faults or exposure — a complete_error is
returned as data for the caller to interpret, so the CLI and Flow can each
own their error vocabulary instead of translating one another's.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Migrate the client CLI onto transport

**Files:**
- Modify: `src/mycelium/client/cli.py`
- Test: `tests/client/test_cli.py` (should need no changes)

**Interfaces:**
- Consumes: `transport.request`, `transport.TransportError`.
- Produces: no API change. `cli.complete()`'s signature, return value and `CompletionError` behavior are all unchanged.

This task's success criterion is that the existing CLI tests pass **untouched**. If a test needs changing, behavior changed, and that is a defect rather than an update — stop and report it.

- [ ] **Step 1: Rewrite `complete`'s body**

In `src/mycelium/client/cli.py`, replace the connect-send-receive block. Everything above it — the ambiguity check and the request dict — stays exactly as it is:

```python
    try:
        message = await transport.request(coordinator_url, coordinator_cert, request, timeout)
    except transport.TransportError as exc:
        raise CompletionError(str(exc)) from None

    if message.get("type") == "complete_result":
        return message["text"]
    if message.get("type") == "complete_error":
        raise CompletionError(message.get("reason", "unknown reason"))
    raise CompletionError(f"unexpected response from coordinator: {message!r}")
```

Update the imports: add `from mycelium.client import transport`, and remove `asyncio`, `websockets` and `build_ssl_context` **only if** nothing else in the file still uses them. `asyncio` is used by `main()`'s `asyncio.run`, so it stays. Check each before deleting.

- [ ] **Step 2: Reconcile the two timeout messages**

The old code raised `f"coordinator did not respond within {timeout}s"` and `"coordinator closed the connection without responding (check --token-file)"`. `TransportError`'s wording is the same for the first and says `(check the token)` rather than `(check --token-file)` for the second.

Read `tests/client/test_cli.py` and check whether any test asserts on that wording. If one does, keep the CLI's message exactly as it was by catching `TransportError` and re-raising with the CLI's own text — the CLI knows about `--token-file` and transport does not, so the more specific message belongs there. Prefer this: it is the reason the caller owns its error vocabulary.

- [ ] **Step 3: Run the whole suite**

Run: `pytest`
Expected: PASS, with `tests/client/test_cli.py` unmodified.

- [ ] **Step 4: Commit**

```bash
git add src/mycelium/client/cli.py
git commit -m "refactor: mycelium-client uses the shared transport (#59)

No behavior change — the existing client tests pass untouched, which is
the evidence. The CLI keeps its own error wording, since it knows about
--token-file and transport does not.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `Flow`, `Hop`, `HopError` and the message helpers

**Files:**
- Create: `src/mycelium/client/flow.py`
- Create: `tests/client/test_flow.py`

**Interfaces:**
- Consumes: `transport.request`, `transport.TransportError`.
- Produces: `Flow(coordinator_url, coordinator_cert, token)`; `await Flow.call(model, *, messages, timeout=...) -> Hop`; `Flow.hops` (list, ordered by index); frozen `Hop`; `HopError(reason, fault, hop)`; `system(content)`, `user(content)`, `assistant(content)`.

`exposure()` is Task 4. Build `call` and the record first.

- [ ] **Step 1: Write the failing tests**

Create `tests/client/test_flow.py`. The load-bearing test captures the **raw frames a fake coordinator received** — not the library's internal state, which would pass even if `call` sent something else.

```python
"""Tests for mycelium.client.flow."""

import asyncio
import json
import ssl

import pytest
import websockets

from mycelium.client.flow import Flow, HopError, assistant, system, user
from mycelium.coordinator import certs, github_identity, server


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


class _FakeCoordinator:
    """A coordinator that records the raw frames it received and replies
    however the test says. Used instead of the real server where a test
    needs to inspect exactly what went on the wire.

    `reply_for` is an async callable taking the received message and
    returning the reply dict, so a test with concurrent calls can key its
    reply off the request rather than off arrival order — each call opens
    its own connection, so a shared queue would race — and can sleep to
    control the order replies come back.
    """

    def __init__(self, reply_for):
        self.received: list[dict] = []
        self._reply_for = reply_for

    async def handler(self, websocket):
        message = json.loads(await websocket.recv())
        self.received.append(message)
        await websocket.send(json.dumps(await self._reply_for(message)))
        await websocket.close()


def _queued(replies):
    """A reply_for that ignores the request and returns each reply in
    order — for tests whose calls are strictly sequential, where arrival
    order is the call order."""
    remaining = list(replies)

    async def reply_for(message):
        return remaining.pop(0)

    return reply_for


def _result(text, node="a3f9c2e1b4d6f8a0", identity="7b1d4408c2e6f1a3"):
    return {
        "type": "complete_result", "text": text,
        "exposed": [{"node_handle": node, "identity_handle": identity}],
        "elapsed_ms": 42,
    }


async def _serve_fake(tmp_path, fake):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    ssl_context = server.build_ssl_context(cert_path, key_path)
    running = await websockets.serve(fake.handler, "127.0.0.1", 0, ssl=ssl_context)
    port = running.sockets[0].getsockname()[1]
    return running, f"wss://127.0.0.1:{port}", cert_path


async def test_only_what_the_call_named_is_sent(tmp_path):
    """THE test for this library: the second hop must carry exactly the
    two messages it named — not the task, not the first hop's input, not
    anything the flow happens to remember. Asserted against the wire,
    because asserting Hop.sent would pass even if call() sent something
    else."""
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a critique")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        draft = await flow.call("small-model", messages=[user("write a haiku about rain")])
        await flow.call(
            "big-model",
            messages=[user("critique this"), assistant(draft.text)],
        )
    finally:
        running.close()
        await running.wait_closed()

    assert fake.received[0]["messages"] == [
        {"role": "user", "content": "write a haiku about rain"}
    ]
    assert fake.received[1]["messages"] == [
        {"role": "user", "content": "critique this"},
        {"role": "assistant", "content": "a draft"},
    ]
    assert "write a haiku about rain" not in json.dumps(fake.received[1]), (
        "the first hop's content must not reach the second unless named"
    )


async def test_the_record_survives_the_caller_mutating_its_list(tmp_path):
    """The record is the evidence behind the exposure report. An agent
    reusing and trimming a message list must not rewrite history."""
    fake = _FakeCoordinator(_queued([_result("ok")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    messages = [user("original")]
    try:
        flow = Flow(url, cert_path, "secret-token")
        hop = await flow.call("m", messages=messages)
    finally:
        running.close()
        await running.wait_closed()

    messages[0]["content"] = "tampered"
    messages.append(user("added later"))

    assert hop.sent == [{"role": "user", "content": "original"}]
    assert flow.hops[0].sent == [{"role": "user", "content": "original"}]


async def test_a_successful_hop_records_everything(tmp_path):
    fake = _FakeCoordinator(_queued([_result("the answer")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        hop = await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    assert hop.index == 0
    assert hop.model == "m"
    assert hop.text == "the answer"
    assert hop.error is None and hop.fault is None
    assert hop.exposed == [
        {"node_handle": "a3f9c2e1b4d6f8a0", "identity_handle": "7b1d4408c2e6f1a3"}
    ]
    assert hop.elapsed_ms == 42
    assert isinstance(hop.wall_ms, int) and hop.wall_ms >= 0
    assert flow.hops == [hop]


async def test_a_failed_hop_is_recorded_then_raises_with_its_fault(tmp_path):
    """#59 requires the record to stay intact and usable after a failure,
    and #58's fault is what lets an agent decide whether to trim its
    context or try a different node."""
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error",
        "reason": "This model's maximum context length is 32768 tokens",
        "fault": "client",
        "exposed": [{"node_handle": "a3f9c2e1b4d6f8a0", "identity_handle": None}],
        "elapsed_ms": 7,
    }])
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        with pytest.raises(HopError) as exc:
            await flow.call("m", messages=[user("far too much")])
    finally:
        running.close()
        await running.wait_closed()

    assert exc.value.fault == "client"
    assert "maximum context length" in str(exc.value)
    assert len(flow.hops) == 1
    recorded = flow.hops[0]
    assert recorded.text is None
    assert recorded.fault == "client"
    assert recorded.sent == [{"role": "user", "content": "far too much"}]
    assert exc.value.hop is recorded


async def test_a_transport_failure_is_recorded_as_a_hop(tmp_path):
    """Nothing reached a node, so no exposure and no elapsed_ms — but the
    attempt is still part of the flow's history."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    flow = Flow("wss://127.0.0.1:1", cert_path, "secret-token")
    with pytest.raises(HopError):
        await flow.call("m", messages=[user("hi")])

    assert len(flow.hops) == 1
    assert flow.hops[0].exposed == []
    assert flow.hops[0].elapsed_ms is None
    assert flow.hops[0].error


def test_the_message_helpers_build_plain_dicts():
    assert system("be terse") == {"role": "system", "content": "be terse"}
    assert user("hi") == {"role": "user", "content": "hi"}
    assert assistant("hello") == {"role": "assistant", "content": "hello"}
```

The transport-failure test connects to port 1, where nothing listens. If that is slow or platform-dependent in this environment, use an unroutable address instead — but keep the assertion that no exposure and no `elapsed_ms` are recorded.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/client/test_flow.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.client.flow'`.

- [ ] **Step 3: Write the module**

```python
"""The primitive agents are written against: a Flow that carries one task
across several models.

There is deliberately no implicit accumulator. Conventional agent
libraries keep conversation state and resend it on every call; here that
would mean every volunteer in a flow sees everything that came before it.
Instead the Flow records each hop locally and sends only what that call
named — see ADR-0004. The property is structural, not a setting: there is
no code path here that sends unnamed content, so it cannot be left
switched off.

The record is never transmitted. It exists for the agent's own logic, for
debugging, and for the exposure report that makes ADR-0004's claim
checkable rather than merely stated. See the design doc for issue #59.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from pathlib import Path

from mycelium.client import transport

# Matches mycelium.client.cli's own default: 10s past the coordinator's
# NODE_COMPLETE_TIMEOUT_SECONDS, so the coordinator's timeout fires first
# and the caller gets its specific reason rather than a vaguer local one.
CALL_TIMEOUT_SECONDS = 140.0


def system(content: str) -> dict:
    """A system message. Exists so ADR-0004's documented example is
    runnable rather than pseudocode."""
    return {"role": "system", "content": content}


def user(content: str) -> dict:
    """A user message."""
    return {"role": "user", "content": content}


def assistant(content: str) -> dict:
    """An assistant message — most often a previous hop's output, passed
    forward deliberately."""
    return {"role": "assistant", "content": content}


@dataclass(frozen=True)
class Hop:
    """One request/response exchange with one node, as the client saw it.

    A failed hop is as complete a record as a successful one: `text` is
    None and `error` is set, but everything else — what was sent, who saw
    it, how long it took — is recorded the same way. Issue #59 requires
    the record to stay intact and usable after a failure.

    `elapsed_ms` is the coordinator's own measure of routing time (issue
    #63) and is None when no node was attempted; `wall_ms` is the whole
    round trip as the client experienced it. Their difference is the
    client-coordinator overhead issue #61 measures.
    """

    index: int
    model: str
    sent: list[dict]
    text: str | None
    error: str | None
    fault: str | None
    exposed: list[dict]
    elapsed_ms: int | None
    wall_ms: int


class HopError(Exception):
    """A hop failed. Carries the node's reason, the `fault` that says whose
    mistake it was (issue #58 — "client" means fix the request, "node"
    means it may be worth trying elsewhere), and the recorded Hop, so a
    handler has the exposure and timing without reaching into flow.hops."""

    def __init__(self, reason: str, fault: str | None, hop: Hop) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fault = fault
        self.hop = hop


class Flow:
    """One logical task spanning several hops, typically across different
    models.

    Holds the coordinator's address and token, and the local record — and
    nothing else. In particular it holds no accumulated conversation:
    `call` sends exactly the messages it is given. An agent that wants a
    previous hop's output in a later hop passes it deliberately.
    """

    def __init__(self, coordinator_url: str, coordinator_cert: Path, token: str) -> None:
        self._coordinator_url = coordinator_url
        self._coordinator_cert = coordinator_cert
        self._token = token
        self._hops: list[Hop] = []
        self._next_index = 0

    @property
    def hops(self) -> list[Hop]:
        """Every hop that has finished, in the order the calls were made.

        Ordered by the index assigned when each call started, not by when
        it completed, so a flow that fans out with asyncio.gather still
        reads back in the agent's own order. A hop appears only once it has
        succeeded or failed — never half-populated.
        """
        return sorted(self._hops, key=lambda hop: hop.index)

    async def call(
        self, model: str, *, messages: list[dict], timeout: float = CALL_TIMEOUT_SECONDS
    ) -> Hop:
        """Send `messages` to `model` and return the recorded Hop.

        Only `messages` is sent. Nothing from earlier hops is added — see
        ADR-0004.

        The messages are deep-copied into the record, so an agent that
        reuses and trims its list across hops cannot retroactively rewrite
        what this hop is recorded as having sent. The record is the
        evidence behind the exposure report; a reference the caller can
        mutate would not be.

        Raises HopError if the hop fails, after recording it. Structural
        validation of `messages` is the coordinator's job (issue #55), not
        duplicated here.
        """
        index = self._next_index
        self._next_index += 1
        sent = copy.deepcopy(messages)
        request = {
            "type": "complete", "token": self._token, "model": model, "messages": messages,
        }

        started = time.monotonic()
        try:
            reply = await transport.request(
                self._coordinator_url, self._coordinator_cert, request, timeout
            )
        except transport.TransportError as exc:
            hop = Hop(
                index=index, model=model, sent=sent, text=None, error=str(exc), fault=None,
                exposed=[], elapsed_ms=None,
                wall_ms=int((time.monotonic() - started) * 1000),
            )
            self._hops.append(hop)
            raise HopError(str(exc), None, hop) from None

        wall_ms = int((time.monotonic() - started) * 1000)
        exposed = reply.get("exposed", [])
        elapsed_ms = reply.get("elapsed_ms")

        if reply.get("type") == "complete_result":
            hop = Hop(
                index=index, model=model, sent=sent, text=reply.get("text"), error=None,
                fault=None, exposed=exposed, elapsed_ms=elapsed_ms, wall_ms=wall_ms,
            )
            self._hops.append(hop)
            return hop

        reason = reply.get("reason", f"unexpected response from coordinator: {reply!r}")
        hop = Hop(
            index=index, model=model, sent=sent, text=None, error=reason,
            fault=reply.get("fault"), exposed=exposed, elapsed_ms=elapsed_ms, wall_ms=wall_ms,
        )
        self._hops.append(hop)
        raise HopError(reason, hop.fault, hop)
```

Note the request carries `messages` (the caller's list), while the record carries `sent` (the deep copy). Serializing happens immediately inside `transport.request`, so the caller cannot mutate the list between here and the wire.

- [ ] **Step 4: Run and confirm they pass, then the whole suite**

Run: `pytest tests/client/test_flow.py -v` then `pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/client/flow.py tests/client/test_flow.py
git commit -m "feat: Flow carries a task across models, sending only what each call names (#59)

No implicit accumulator — the property ADR-0004 requires is structural,
because there is no code path that sends unnamed content. The record deep-
copies what it sent, so an agent trimming its context cannot rewrite the
evidence behind the exposure report.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `exposure()`, concurrency ordering, and the import surface

**Files:**
- Modify: `src/mycelium/client/flow.py`
- Modify: `src/mycelium/client/__init__.py`
- Test: `tests/client/test_flow.py`

**Interfaces:**
- Produces: frozen `Exposure(by_node, by_identity)`; `Flow.exposure() -> Exposure`; `from mycelium.client import Flow, Hop, HopError, Exposure, system, user, assistant`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_exposure_groups_by_node_and_by_identity(tmp_path):
    """Two nodes under one identity is the case the report exists for:
    the per-identity cap is three, so three nodes can be one person."""
    fake = _FakeCoordinator(_queued([
        _result("one", node="node-aaaa", identity="ident-1111"),
        _result("two", node="node-bbbb", identity="ident-1111"),
        _result("three", node="node-aaaa", identity="ident-1111"),
    ])
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        for _ in range(3):
            await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    exposure = flow.exposure()
    assert exposure.by_node == {"node-aaaa": [0, 2], "node-bbbb": [1]}
    assert exposure.by_identity == {"ident-1111": [0, 1, 2]}, (
        "three hops across two nodes were all seen by one volunteer"
    )


async def test_exposure_keeps_an_unresolved_identity_under_none(tmp_path):
    """A node whose identity could not be resolved still saw the hop —
    dropping it would under-report exposure."""
    fake = _FakeCoordinator(_queued([_result("one", node="node-aaaa", identity=None)]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    assert flow.exposure().by_identity == {None: [0]}


async def test_exposure_counts_a_failed_hop(tmp_path):
    """The node read the conversation before rejecting it."""
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error", "reason": "too long", "fault": "client",
        "exposed": [{"node_handle": "node-aaaa", "identity_handle": "ident-1111"}],
        "elapsed_ms": 5,
    }])
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        with pytest.raises(HopError):
            await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    assert flow.exposure().by_node == {"node-aaaa": [0]}


async def test_a_failover_hop_reports_both_nodes(tmp_path):
    """#63 makes a hop's exposure a list when the coordinator failed over."""
    fake = _FakeCoordinator(_queued([{
        "type": "complete_result", "text": "ok",
        "exposed": [
            {"node_handle": "node-aaaa", "identity_handle": "ident-1111"},
            {"node_handle": "node-bbbb", "identity_handle": "ident-2222"},
        ],
        "elapsed_ms": 9,
    }])
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await flow.call("m", messages=[user("hi")])
    finally:
        running.close()
        await running.wait_closed()

    exposure = flow.exposure()
    assert exposure.by_node == {"node-aaaa": [0], "node-bbbb": [0]}
    assert exposure.by_identity == {"ident-1111": [0], "ident-2222": [0]}


async def test_concurrent_calls_are_recorded_in_call_order(tmp_path):
    """Fanning one question out to several models is a plausible agent
    shape; the record must read back in the agent's order, not the order
    the models happened to answer.

    The fake answers slowest for the call made FIRST, so completion order
    is the reverse of call order. A test whose replies came back in call
    order would pass even if hops were appended on completion with no
    index — this one only passes if the index assigned at call time is
    what orders the record.
    """

    async def slow_reversed(message):
        content = message["messages"][0]["content"]
        await asyncio.sleep({"a": 0.15, "b": 0.10, "c": 0.05}[content])
        return _result(f"reply to {content}")

    fake = _FakeCoordinator(slow_reversed)
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = Flow(url, cert_path, "secret-token")
        await asyncio.gather(
            flow.call("m", messages=[user("a")]),
            flow.call("m", messages=[user("b")]),
            flow.call("m", messages=[user("c")]),
        )
    finally:
        running.close()
        await running.wait_closed()

    assert [hop.index for hop in flow.hops] == [0, 1, 2]
    assert [hop.sent[0]["content"] for hop in flow.hops] == ["a", "b", "c"], (
        "the record must follow call order, not the order replies arrived"
    )


def test_the_public_surface_is_importable_from_the_package():
    from mycelium.client import Exposure, Flow, Hop, HopError, assistant, system, user

    assert all([Exposure, Flow, Hop, HopError, assistant, system, user])
```

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/client/test_flow.py -k "exposure or concurrent or public_surface" -v`
Expected: FAIL — `Flow` has no `exposure`, and the package exports nothing.

- [ ] **Step 3: Add `Exposure` and `exposure()`**

In `src/mycelium/client/flow.py`, after `Hop`:

```python
@dataclass(frozen=True)
class Exposure:
    """Which volunteers saw which hops of one flow.

    Both groupings are reported because both questions matter and neither
    answers the other: `by_node` says how much one machine saw, and
    `by_identity` says how much one *person* saw — the per-identity cap is
    three, so three different nodes can be a single volunteer. That is the
    aggregation ADR-0004 exists to bound.

    A None key in `by_identity` is a node whose bound identity could not be
    resolved. It is kept rather than dropped: that node still saw the hop,
    and omitting it would under-report exposure.
    """

    by_node: dict[str, list[int]]
    by_identity: dict[str | None, list[int]]
```

and the method on `Flow`:

```python
    def exposure(self) -> Exposure:
        """Group this flow's hops by the volunteer that saw them.

        Counts failed hops exactly like successful ones — a node that read
        the conversation and then rejected or dropped it still read it —
        and counts every entry of a hop's `exposed` list, which holds more
        than one node when the coordinator failed over (issue #63).

        Returns data, not a rendering: formatting belongs to the agent.
        """
        by_node: dict[str, list[int]] = {}
        by_identity: dict[str | None, list[int]] = {}
        for hop in self.hops:
            for entry in hop.exposed:
                by_node.setdefault(entry["node_handle"], []).append(hop.index)
                by_identity.setdefault(entry.get("identity_handle"), []).append(hop.index)
        return Exposure(by_node=by_node, by_identity=by_identity)
```

- [ ] **Step 4: Export the public surface**

Replace `src/mycelium/client/__init__.py` with:

```python
"""The client-side surface an agent author writes against.

Re-exported here so `mycelium.client.flow` and `mycelium.client.transport`
stay free to change shape — the package's stated design is a small set of
primitives, not a framework, and these seven names are all of it. See the
design doc for issue #59.
"""

from mycelium.client.flow import (
    Exposure,
    Flow,
    Hop,
    HopError,
    assistant,
    system,
    user,
)

__all__ = ["Exposure", "Flow", "Hop", "HopError", "assistant", "system", "user"]
```

Check what the file currently holds before overwriting — if it carries a docstring or anything else, preserve it.

- [ ] **Step 5: Run the whole suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/mycelium/client/flow.py src/mycelium/client/__init__.py tests/client/test_flow.py
git commit -m "feat: per-node and per-identity exposure report (#59)

Both groupings, because neither answers the other: three different nodes
can be one volunteer, which is the aggregation ADR-0004 exists to bound.
Failed hops count, and an unresolved identity is kept under a None key
rather than dropped — omitting it would under-report exposure.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Document it

**Files:**
- Modify: `docs/OPERATIONS.md`

- [ ] **Step 1: Read the surrounding sections**

`docs/OPERATIONS.md` runs `## Step 1` … `## Step 6`, then `## Troubleshooting`. `## Step 5 — Send a completion as a client` (line ~279) already has a `### Sending a multi-turn conversation` subsection from #55.

Read Step 5 in full. The new material is a sibling subsection at the end of Step 5, after the multi-turn one — it is still "sending completions as a client", just programmatically.

- [ ] **Step 2: Add the section**

````markdown
### Writing an agent that uses several models

For a task that needs more than one model — a small fast model to draft and
a larger one to critique, say — the client library carries the task across
hops while you decide what each hop sees:

```python
import asyncio
from pathlib import Path
from mycelium.client import Flow, HopError, user, assistant

async def main():
    flow = Flow(
        "wss://coordinator.example:8765",
        Path("coordinator.pem"),
        Path("client-token.txt").read_text().strip(),
    )
    task = "Explain why the sky is blue, in two sentences."

    draft = await flow.call("Qwen/Qwen2.5-1.5B-Instruct", messages=[user(task)])
    review = await flow.call(
        "Qwen/Qwen2.5-7B-Instruct",
        messages=[user(f"Improve this answer to: {task}"), assistant(draft.text)],
    )
    print(review.text)

    for handle, hops in flow.exposure().by_identity.items():
        print(f"identity {handle} saw {len(hops)} of {len(flow.hops)} hops")

asyncio.run(main())
```

**Nothing is sent that a call did not name.** There is no accumulated
conversation behind `flow`: the second call above sends exactly the two
messages it lists, and the volunteer serving it never sees the first hop's
prompt unless you pass it. This is structural rather than a setting — the
library has no code path that sends unnamed content — and it is why passing
a previous hop's output is something you write out explicitly.

`flow.hops` is the local record of every hop: what was sent, what came back,
which volunteer served it, and how long it took. It is never transmitted.

`flow.exposure()` reports which volunteers saw which hops, grouped two ways.
`by_node` tells you how much one machine saw; `by_identity` tells you how
much one *person* saw — a single volunteer may run several nodes, so those
are different questions. A `None` identity key is a node whose account the
coordinator could not resolve; it still saw the hop.

A failed hop raises `HopError` and stays in the record:

```python
try:
    await flow.call("Qwen/Qwen2.5-7B-Instruct", messages=[user(enormous)])
except HopError as exc:
    if exc.fault == "client":
        ...  # your request was the problem — trim it and try again
    else:
        ...  # the node's problem — another model or another attempt
    print(exc.hop.exposed)  # that volunteer read it regardless
```

Keeping a conversation within a model's context window is your job. The node
never silently truncates.
````

Do not describe the handles as making inference private. They narrow how much
each volunteer sees, not whether they see it — the same wording discipline the
rest of this file already uses.

- [ ] **Step 3: Verify the example actually imports**

Run: `python -c "from mycelium.client import Flow, HopError, user, assistant; print('ok')"`
Expected: `ok`. If it fails, the example is wrong — fix the example or the exports, not the documentation's claim.

- [ ] **Step 4: Commit and open the PR**

```bash
git add docs/OPERATIONS.md
git commit -m "docs: writing an agent against several models (#59)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin phase-2/issue-59-flow-library
```

Open the PR against `main`, titled `Multi-hop client library with explicit per-hop context (#59)`. The body should state: that the explicit-context property is structural because no code path sends unnamed content; that the record deep-copies so it cannot be retroactively rewritten; that both exposure groupings are reported and why neither answers the other; that `HopError` carries `fault` so an agent can tell "fix my request" from "try elsewhere"; and that `list_models()` is deliberately absent until #56 lands.

---

## Acceptance criteria mapping

| #59 criterion | Where it is met |
|---|---|
| Two models in sequence, second carrying content explicitly passed | Task 3 Step 1 (`test_only_what_the_call_named_is_sent`) |
| Nothing sent that the call did not name, verifiable by inspection | Task 3 Step 1 — asserted against the frames a fake coordinator received |
| Complete local record: sent, received, which node served it | Task 3 Step 1 (`..._records_everything`), Task 4 |
| Per-node and per-identity exposure | Task 4 Step 1 |
| A failed hop surfaces with its reason, record intact | Task 3 Step 1 (`..._recorded_then_raises_with_its_fault`) |
| `docs/OPERATIONS.md` documents the library and the rule | Task 5 |

## Notes for whoever picks this up

- The explicit-context test must assert against what the fake coordinator *received*. A test reading `Hop.sent` proves only that the record matches itself.
- Do not add `list_models()`. The wire request does not exist until #56, and the method is additive when it does.
- Do not validate `messages` client-side. #55 made the coordinator the single owner of that rule.
- Do not touch `status_cli.py` or `ban_cli.py`. They hand-roll their own round trip; migrating them is a different concern from this phase.
- Task 2's success criterion is that `tests/client/test_cli.py` passes **unmodified**. If it needs edits, behavior changed — stop and report rather than updating the test.
