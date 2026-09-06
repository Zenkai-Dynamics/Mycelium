# Reference Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One runnable agent that uses two genuinely different models in a single flow, proving the Phase 2 primitives compose and that writing an agent under the explicit-context rule is workable.

**Architecture:** A single file in `examples/`, outside the installed package. `run()` performs the two hops and returns the `Flow` so tests can assert structurally; `main()` parses arguments, prints, and sets the exit status. Tests load the file by path and drive it against a frame-recording fake coordinator.

**Tech Stack:** Python 3.11+, the `mycelium.client` package (`Flow`, `HopError`, `user`, `assistant`), stdlib `argparse`/`asyncio`/`importlib`, `pytest` 9.1.1 with `pytest-asyncio` in `asyncio_mode = "auto"`.

## Global Constraints

- Issue: [#60](https://github.com/Zenkai-Dynamics/Mycelium/issues/60). Parent: [#54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54). Blocks #61.
- Design authority: `docs/superpowers/specs/2026-09-05-issue-60-reference-agent-design.md`. Where this plan and that document disagree, the document wins.
- Branch: `phase-2/issue-60-reference-agent`, off `main`. One PR at the end.
- **No changes under `src/mycelium/`.** This issue consumes the library; it does not modify it. If the example seems to need a library change, stop and report it — that is a finding about #59, not work for this issue.
- The example lives in `examples/`, outside the installed package. Do not add it to `pyproject.toml`'s packages or scripts.
- The critique hop **is** given the task, deliberately, and the example prints that reasoning. This is not an oversight to correct.
- When a hop fails with `fault is None`, the example must state that the exposure figures are a floor rather than a count.
- No test may make a real network call to GitHub or start a real vLLM. Fakes only.
- Style: match the surrounding code — long docstrings recording *why*, comments citing the issue number whose design doc explains a decision.
- Run the whole suite (`pytest`) before every commit. Every commit leaves it green. The suite is currently at 335 passing.

---

### Task 1: The flow — `run()` and its tests

**Files:**
- Create: `examples/draft_then_critique.py`
- Create: `tests/examples/__init__.py`
- Create: `tests/examples/test_draft_then_critique.py`

**Interfaces:**
- Produces: `run(coordinator_url: str, coordinator_cert: Path, token: str, draft_model: str, critique_model: str, task: str, flow: Flow | None = None) -> Flow` (async), plus `DEFAULT_DRAFT_MODEL`, `DEFAULT_CRITIQUE_MODEL`, `DEFAULT_TASK` module constants.

The optional `flow` parameter matters more than it looks: when a hop fails, `run` raises and returns nothing, so a caller that did not already hold the `Flow` loses the record of everything that happened before the failure. Letting the caller construct it means the record survives a failure in the caller's hands — which is exactly what #59 built the record to do.

`main()`, argparse and printing are Task 2.

- [ ] **Step 1: Write the failing tests**

`tests/examples/__init__.py` is empty — the other test directories have one, so this matches.

Create `tests/examples/test_draft_then_critique.py`:

```python
"""Tests for examples/draft_then_critique.py.

The example lives outside the installed package deliberately (see the
design doc for issue #60), so it is loaded by path rather than imported.
It is tested anyway because it is a consumer of the Flow API: on the #55
branch a public signature changed and a caller outside every task's slice
was missed, shipping a broken operator command. An untested example rots
the same way, and the most expensive place to discover that is #61's
live-hardware session.

The fake-coordinator helpers are duplicated from tests/client/test_flow.py
rather than shared, matching this repo's existing precedent for test
helpers (_client_ssl_context and _fake_identity_verifier are each copied
across three test files).
"""

import importlib.util
import json
from pathlib import Path

import pytest
import websockets

from mycelium.coordinator import certs, server

MODULE_PATH = Path(__file__).parents[2] / "examples" / "draft_then_critique.py"


def _load_example():
    """Load the example by path. examples/ is not an installed package and
    must not become one, so there is nothing to import."""
    spec = importlib.util.spec_from_file_location("draft_then_critique", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCoordinator:
    """Records the raw frames it received and replies however the test
    says — so a test can assert exactly what each hop put on the wire."""

    def __init__(self, reply_for):
        self.received: list[dict] = []
        self._reply_for = reply_for

    async def handler(self, websocket):
        message = json.loads(await websocket.recv())
        self.received.append(message)
        await websocket.send(json.dumps(await self._reply_for(message)))
        await websocket.close()


def _queued(replies):
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


async def test_the_flow_calls_both_models_in_order(tmp_path):
    agent = _load_example()
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a better draft")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        flow = await agent.run(
            url, cert_path, "secret-token",
            draft_model="small-model", critique_model="big-model",
            task="Why is the sky blue?",
        )
    finally:
        running.close()
        await running.wait_closed()

    assert [hop.model for hop in flow.hops] == ["small-model", "big-model"]
    assert flow.hops[1].text == "a better draft"


async def test_the_critique_hop_carries_exactly_what_it_named(tmp_path):
    """The load-bearing assertion: exact equality, not 'contains the
    draft'. A containment check would pass against an implementation that
    also resent the first hop's messages."""
    agent = _load_example()
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a better draft")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        await agent.run(
            url, cert_path, "secret-token",
            draft_model="small-model", critique_model="big-model",
            task="Why is the sky blue?",
        )
    finally:
        running.close()
        await running.wait_closed()

    assert fake.received[0]["messages"] == [
        {"role": "user", "content": "Why is the sky blue?"}
    ]
    assert fake.received[1]["messages"] == [
        {"role": "user", "content": "Improve this answer to: Why is the sky blue?"},
        {"role": "assistant", "content": "a draft"},
    ]


async def test_the_models_are_the_ones_asked_for(tmp_path):
    agent = _load_example()
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a better draft")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        await agent.run(
            url, cert_path, "secret-token",
            draft_model="tiny", critique_model="huge",
            task="anything",
        )
    finally:
        running.close()
        await running.wait_closed()

    assert [frame["model"] for frame in fake.received] == ["tiny", "huge"]


async def test_a_failed_hop_propagates_as_a_hop_error(tmp_path):
    """run() does not swallow failures — main() decides what to do with
    them, so the flow function stays reusable."""
    from mycelium.client import HopError

    agent = _load_example()
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error", "reason": "context too long",
        "fault": "client", "exposed": [], "elapsed_ms": 3,
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)

    try:
        with pytest.raises(HopError) as exc:
            await agent.run(
                url, cert_path, "secret-token",
                draft_model="small-model", critique_model="big-model",
                task="anything",
            )
    finally:
        running.close()
        await running.wait_closed()

    assert exc.value.fault == "client"


def test_the_defaults_name_two_different_models():
    agent = _load_example()

    assert agent.DEFAULT_DRAFT_MODEL != agent.DEFAULT_CRITIQUE_MODEL
    assert agent.DEFAULT_TASK
```

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/examples/ -v`
Expected: FAIL — the example file does not exist, so `spec_from_file_location` returns a spec whose loader cannot find it (`FileNotFoundError`).

- [ ] **Step 3: Write the example's flow half**

Create `examples/draft_then_critique.py`:

```python
"""Draft, then critique: one task across two models.

A small fast model writes a first answer; a larger one improves it. Both
models demonstrably contribute, and the second hop needs content from the
first — which is exactly the property this example exists to demonstrate.

Mycelium ships primitives plus this one demonstration, not an agent
framework. That is why this file lives in examples/ rather than in the
installed package: roles, planning and tool-calling abstractions belong
to something like LangGraph, not to an inference router. See the design
doc for issue #60.

**What each hop carries is a decision, not a default.** The critique hop
is given the original task as well as the draft, because a critic that
cannot see the task cannot judge whether the draft answers it. That means
the second volunteer sees the task too, and the exposure report this
example prints will say so. Explicit context (ADR-0004) does not mean
"send less" — it means decide per hop, and be able to see what you
decided. Nothing is sent that a call did not name.

Run it against your own nodes:

    python examples/draft_then_critique.py \\
        --coordinator-url wss://coordinator.example:8765 \\
        --coordinator-cert coordinator.pem \\
        --token-file client-token.txt
"""

from __future__ import annotations

from pathlib import Path

from mycelium.client import Flow, assistant, user

# The pair issue #61 plans to run on real hardware: a ~3GB instruct model
# alongside the 7B already served there. Overridable, so the example runs
# against whatever a reader actually serves.
DEFAULT_DRAFT_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DEFAULT_CRITIQUE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_TASK = "Explain why the sky is blue, in two sentences."


async def run(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    draft_model: str,
    critique_model: str,
    task: str,
    flow: Flow | None = None,
) -> Flow:
    """Draft with one model, improve with another, and return the Flow.

    Returns rather than prints so the flow can be inspected — by main()
    below, and by the tests. A failed hop propagates as HopError; deciding
    what to do about it belongs to the caller, not here.

    `flow` lets the caller own the record. That matters on failure: this
    function raises rather than returning, so a caller that did not
    already hold the Flow would lose the history of everything that
    happened before the failure — including which volunteers had already
    seen content. Passing one in keeps that record in the caller's hands,
    which is what issue #59 built it for.
    """
    flow = flow or Flow(coordinator_url, coordinator_cert, token)

    draft = await flow.call(draft_model, messages=[user(task)])

    # The task goes to the critique hop as well as the draft — deliberately.
    # A critic that cannot see the task cannot judge whether the draft
    # answers it. See the design doc for issue #60 on why this is the
    # honest demonstration of the explicit-context rule rather than a
    # violation of it.
    await flow.call(
        critique_model,
        messages=[user(f"Improve this answer to: {task}"), assistant(draft.text)],
    )

    return flow
```

- [ ] **Step 4: Run and confirm they pass, then the whole suite**

Run: `pytest tests/examples/ -v` then `pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/draft_then_critique.py tests/examples/
git commit -m "feat: reference agent's two-model flow (#60)

Draft with a small model, improve with a larger one. The critique hop is
given the task as well as the draft, deliberately — a critic that cannot
see the task cannot judge whether the draft answers it. Explicit context
means deciding per hop, not sending less.

run() returns the Flow rather than printing, so the tests can assert what
each hop put on the wire rather than pattern-matching output.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `main()` — arguments, output and exit status

**Files:**
- Modify: `examples/draft_then_critique.py`
- Modify: `tests/examples/test_draft_then_critique.py`

**Interfaces:**
- Consumes: `run()` from Task 1.
- Produces: `parse_args(argv)`, `main(argv=None) -> int`, and the `if __name__ == "__main__": sys.exit(main())` guard.

- [ ] **Step 1: Write the failing tests**

Append to `tests/examples/test_draft_then_critique.py`:

```python
def test_parse_args_defaults_to_the_documented_models(tmp_path):
    agent = _load_example()

    args = agent.parse_args([
        "--coordinator-url", "wss://x", "--coordinator-cert", "c.pem",
        "--token-file", "t.txt",
    ])

    assert args.draft_model == agent.DEFAULT_DRAFT_MODEL
    assert args.critique_model == agent.DEFAULT_CRITIQUE_MODEL
    assert args.task == agent.DEFAULT_TASK


def test_parse_args_requires_the_connection_flags():
    agent = _load_example()

    with pytest.raises(SystemExit):
        agent.parse_args([])


async def test_main_prints_what_each_hop_sent(tmp_path, capsys):
    """Surfacing what each hop carried is an acceptance criterion in its
    own right — a reader must be able to see the explicit-context rule in
    practice, not just read about it."""
    agent = _load_example()
    fake = _FakeCoordinator(_queued([_result("a draft"), _result("a better draft")]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret-token")

    try:
        status = await asyncio.to_thread(agent.main, [
            "--coordinator-url", url,
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
            "--draft-model", "small-model",
            "--critique-model", "big-model",
            "--task", "Why is the sky blue?",
        ])
    finally:
        running.close()
        await running.wait_closed()

    assert status == 0
    out = capsys.readouterr().out
    assert "Why is the sky blue?" in out
    assert "Improve this answer to: Why is the sky blue?" in out
    assert "a draft" in out
    assert "a better draft" in out


async def test_main_reports_a_failed_hop_without_a_traceback(tmp_path, capsys):
    agent = _load_example()
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error",
        "reason": "This model's maximum context length is 32768 tokens",
        "fault": "client", "exposed": [], "elapsed_ms": 3,
    }]))
    running, url, cert_path = await _serve_fake(tmp_path, fake)
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret-token")

    try:
        status = await asyncio.to_thread(agent.main, [
            "--coordinator-url", url,
            "--coordinator-cert", str(cert_path),
            "--token-file", str(token_file),
        ])
    finally:
        running.close()
        await running.wait_closed()

    assert status == 1
    out = capsys.readouterr().out
    assert "maximum context length" in out
    assert "client" in out


async def test_main_says_exposure_is_a_floor_when_contact_was_lost(tmp_path, capsys):
    """fault is None means the client never learned who saw its content —
    so the exposure figures understate it, and saying otherwise would
    overclaim. See the design doc for issue #59."""
    agent = _load_example()
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret-token")
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    # Nothing is listening on this port, so the hop fails in transport.
    status = await asyncio.to_thread(agent.main, [
        "--coordinator-url", "wss://127.0.0.1:1",
        "--coordinator-cert", str(cert_path),
        "--token-file", str(token_file),
    ])

    assert status == 1
    assert "floor" in capsys.readouterr().out
```

Add `import asyncio` to the test file's imports — `main()` is synchronous and calls `asyncio.run` internally, so it must be driven off the event loop with `asyncio.to_thread` from these async tests.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/examples/ -v`
Expected: FAIL — `module 'draft_then_critique' has no attribute 'parse_args'`.

- [ ] **Step 3: Write `main()`**

Add to `examples/draft_then_critique.py`. Extend the imports first:

```python
import argparse
import asyncio
import sys
from pathlib import Path

from mycelium.client import Flow, HopError, assistant, user
```

and append below `run`:

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="draft_then_critique",
        description="Draft an answer with one model, improve it with another.",
    )
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--draft-model", default=DEFAULT_DRAFT_MODEL)
    parser.add_argument("--critique-model", default=DEFAULT_CRITIQUE_MODEL)
    parser.add_argument("--task", default=DEFAULT_TASK)
    return parser.parse_args(argv)


def _print_hops(flow: Flow) -> None:
    """Show what each hop actually sent, so the explicit-context rule is
    visible in practice rather than only described."""
    for hop in flow.hops:
        print(f"\nhop {hop.index} -> {hop.model}")
        for message in hop.sent:
            print(f"  sent {message['role']}: {message['content']}")
        if hop.text is not None:
            print(f"  got: {hop.text}")


def _print_exposure(flow: Flow, contact_lost: bool) -> None:
    exposure = flow.exposure()
    print("\nwho saw what:")
    for handle, hops in exposure.by_node.items():
        print(f"  node {handle} saw hops {hops}")
    for handle, hops in exposure.by_identity.items():
        who = "an unresolved identity" if handle is None else f"identity {handle}"
        print(f"  {who} saw hops {hops}")
    if contact_lost:
        # The client never learned who served the failed hop, so anyone
        # the coordinator had already routed to is missing from the count.
        # See the design doc for issue #59 on why this is a floor.
        print(
            "\n  note: contact was lost mid-flow, so these figures are a "
            "floor, not a count — the coordinator may already have sent "
            "your content to a volunteer this client never heard about."
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.token_file.read_text().strip()

    # Constructed here, not inside run(), so the record survives a failed
    # hop: run() raises rather than returning, and without holding the
    # Flow already this function could not report which volunteers had
    # seen content before things went wrong. See the design doc for #60.
    flow = Flow(args.coordinator_url, args.coordinator_cert, token)

    try:
        asyncio.run(run(
            args.coordinator_url, args.coordinator_cert, token,
            args.draft_model, args.critique_model, args.task, flow,
        ))
    except HopError as exc:
        print(f"\nhop failed: {exc.reason}")
        if exc.fault == "client":
            print("  fault: client — the request was the problem, not the node")
        elif exc.fault == "node":
            print("  fault: node — the volunteer's machine failed, not your request")
        else:
            print("  fault: unknown — the request never reached a node")
        _print_hops(flow)
        _print_exposure(flow, contact_lost=exc.fault is None)
        return 1

    _print_hops(flow)
    _print_exposure(flow, contact_lost=False)
    print(f"\nresult:\n{flow.hops[-1].text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run and confirm they pass, then the whole suite**

Run: `pytest tests/examples/ -v` then `pytest`
Expected: PASS.

- [ ] **Step 5: Verify the example's own help text works**

Run: `python examples/draft_then_critique.py --help`
Expected: usage text listing all six flags, with the two model defaults visible. If it errors, the file is not runnable as documented and that must be fixed before committing.

- [ ] **Step 6: Commit**

```bash
git add examples/draft_then_critique.py tests/examples/test_draft_then_critique.py
git commit -m "feat: reference agent prints what each hop sent (#60)

A failed hop branches on fault rather than surfacing a traceback, and
when contact was lost mid-flow the exposure figures are labelled a floor
rather than a count — the client never learned who the coordinator had
already routed to.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Point at it from the operations guide

**Files:**
- Modify: `docs/OPERATIONS.md`

- [ ] **Step 1: Find the client-library section**

Run: `grep -n "Writing an agent that uses several models" docs/OPERATIONS.md`

That subsection was added by #59 and sits at the end of Step 5. Read it before editing.

- [ ] **Step 2: Add the pointer**

At the end of that subsection, after the existing text, add:

```markdown
A complete, runnable version of this flow ships in
`examples/draft_then_critique.py`. It takes both model names as arguments,
prints what every hop sent, and reports which volunteers saw which hops —
run it with `--help` to see the options. It is deliberately an example
rather than part of the installed package: Mycelium ships primitives and
one demonstration, not an agent framework.
```

Do not restructure or reword anything already in that section.

- [ ] **Step 3: Verify the path is right**

Run: `ls examples/draft_then_critique.py`
Expected: the file exists at exactly that path. A documentation pointer to a file that is not there is worse than no pointer.

- [ ] **Step 4: Commit and open the PR**

```bash
git add docs/OPERATIONS.md
git commit -m "docs: point at the reference agent from the operations guide (#60)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin phase-2/issue-60-reference-agent
```

Open the PR against `main`, titled `Reference agent demonstrating a two-model flow (#60)`. The body should state: what the flow does; that the critique hop is given the task deliberately and why that is the honest demonstration of explicit context rather than a violation; that the example is tested despite living outside the package, and why this project's history makes that the right call; and that a lost-contact failure labels the exposure figures a floor rather than a count.

---

## Acceptance criteria mapping

| #60 criterion | Where it is met |
|---|---|
| A runnable example performs a real two-model flow end to end | Task 1 Step 1 (`..._calls_both_models_in_order`), live-confirmed in #61 |
| The second hop uses content from the first, passed explicitly | Task 1 Step 1 (`..._carries_exactly_what_it_named`) |
| It prints what each hop sent | Task 2 Step 1 (`test_main_prints_what_each_hop_sent`) |
| A failed hop is handled without a traceback | Task 2 Step 1 (`..._without_a_traceback`) |
| It lives in `examples/`, not the installed package | Task 1 Step 3; nothing added to `pyproject.toml` |
| Documented well enough to run against your own nodes | Module docstring, `--help` (Task 2 Step 6), and Task 3 |

## Notes for whoever picks this up

- Do not change anything under `src/mycelium/`. If the example appears to need a library change, that is a finding about #59 — stop and report it rather than editing the library.
- The critique hop receiving the task is deliberate. Do not "fix" it to withhold the task; the example prints the reasoning precisely because a reader might expect otherwise from ADR-0004's illustrative sample.
- `main()` constructs the `Flow` and passes it into `run()`. That is not incidental: `run()` raises on a failed hop rather than returning, so without holding the Flow already, `main()` could not report which volunteers had seen content before the failure.
- The exact-equality assertion in `..._carries_exactly_what_it_named` is the point of the whole test file. A containment check would pass against an implementation that also resent the first hop.
