# Issue #12 — Clean, Immediate Failure When No Healthy Node Is Available — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lock in, with a dedicated regression test, that a client request for a model with zero currently-healthy nodes fails immediately with a clear error and no retry/queueing. This closes Phase 0 success criterion #4.

**Architecture:** No production code changes. `NodeRegistry.find_node_for_model` already returns `None` on the first (and only) lookup for a model nobody has registered, `server._handle_complete_request` already raises `NoHealthyNodeError` and replies with `complete_error` immediately — built and live-verified as part of #10. This plan adds one test that proves it structurally (call-count, not just timing) and formalizes the record in `phase-0-foundation.md`.

**Tech Stack:** Python 3.11+, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`, already configured), `websockets` 17.x. No new dependencies.

## Global Constraints

- **Source of truth:** `docs/superpowers/specs/2026-08-16-issue-12-clean-immediate-failure-no-healthy-node-design.md`. If anything in this plan seems to contradict it, the design doc wins.
- No changes to `router.py`, `registry.py`, or `server.py` — this issue is test-and-docs only.
- Based on current `main`, independent of #11 (still an open PR at the time this plan is written) — do not assume #11's `NodeRegistry.find_node_for_model(model, exclude=...)` signature or its retry loop in `server.py` exist.
- "No retry" must be confirmed structurally (an invocation-count assertion), not by wall-clock timing alone — timing assertions alone would still pass if a future change added a short retry/poll loop.
- No new live-hardware verification — #10's own PR already exercised this exact scenario (empty registry, real client, real coordinator) on real hardware.

---

### Task 1: Regression test — no healthy node fails fast with exactly one lookup, no retry

**Files:**
- Modify: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `mycelium.coordinator.registry.NodeRegistry` (imported fresh into this test file — not currently imported there on `main`), `server.serve` (existing).
- Produces: nothing consumed elsewhere — this is a leaf regression test.

- [ ] **Step 1: Write the failing test**

In `tests/coordinator/test_server.py`, add to the imports — replace:

```python
from mycelium.coordinator import certs, router, server
```

with:

```python
from mycelium.coordinator import certs, router, server
from mycelium.coordinator.registry import NodeRegistry
```

Then, immediately after the existing `test_complete_request_with_no_matching_node_returns_error` test, add:

```python
async def test_complete_request_with_no_healthy_node_fails_fast_with_no_retry(tmp_path, monkeypatch):
    call_count = 0
    original_find_node_for_model = NodeRegistry.find_node_for_model

    def counting_find_node_for_model(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_find_node_for_model(self, *args, **kwargs)

    monkeypatch.setattr(NodeRegistry, "find_node_for_model", counting_find_node_for_model)

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 0, cert_path, key_path, "secret-token") as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "no-such-model", "prompt": "hi"}
            ))
            start = time.monotonic()
            response = json.loads(await client_ws.recv())
            elapsed = time.monotonic() - start

    assert response["type"] == "complete_error"
    assert "no-such-model" in response["reason"]
    assert elapsed < 1.0, (
        f"response took {elapsed:.2f}s — a missing-node error must be immediate, not a hang or a wait"
    )
    assert call_count == 1, (
        f"find_node_for_model was called {call_count} times — expected exactly 1 (no retry/poll loop)"
    )
```

This monkeypatches the `NodeRegistry` class method (not a specific instance — `server.serve` creates its own `NodeRegistry` internally and doesn't expose it, so class-level patching is the only way to observe call count from outside).

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/coordinator/test_server.py::test_complete_request_with_no_healthy_node_fails_fast_with_no_retry -v`
Expected: FAILS with `ImportError`/`ModuleNotFoundError`-style collection error if the import wasn't added correctly, or — if the import is fine — the test should actually already PASS against current `main`'s code, since #10 already implements the immediate-single-lookup behavior this test checks. This is expected and fine: this task's job is to add a regression test locking in existing correct behavior, not to fix a bug. If it fails for any reason other than a typo/import issue, stop and investigate before proceeding — that would mean the assumed-correct existing behavior isn't actually correct, which is new information the design doc didn't anticipate.

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: PASS. The added `NodeRegistry` import doesn't shadow or conflict with anything else in the file (`certs`, `router`, `server` remain module-level imports as before).

- [ ] **Step 4: Commit**

```bash
git add tests/coordinator/test_server.py
git commit -m "test: confirm a missing-model request fails immediately with no retry"
```

---

### Task 2: Update `docs/phases/phase-0-foundation.md`

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
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) · [Node agent vLLM wrapper](../superpowers/specs/2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) · [Node registration handshake](../superpowers/specs/2026-08-15-issue-8-node-registration-handshake-design.md) · [Node heartbeat & liveness tracking](../superpowers/specs/2026-08-15-issue-9-node-heartbeat-liveness-design.md) · [Coordinator forwards a client request](../superpowers/specs/2026-08-15-issue-10-coordinator-forwards-client-request-design.md) · [Clean, immediate failure when no healthy node is available](../superpowers/specs/2026-08-16-issue-12-clean-immediate-failure-no-healthy-node-design.md)
```

Note: this does not add issue #11's design doc link — #11 is a separate, still-open PR at the time this plan is written; leave that out of scope for this task.

- [ ] **Step 2: Add a new bullet to `## Open risks / unresolved decisions`**

After the `**Client request routing: resolved.**` bullet (the last one currently in the file), add:

```markdown
- **Immediate failure with no healthy node: resolved.** A client request for a model with zero currently-registered nodes fails on the very first, single `NodeRegistry.find_node_for_model` lookup — no wait, no queue, no retry. Built and live-verified as part of #10 (`error: no healthy node for model '...'`, 0.111s, exit 1, against a real coordinator with no nodes registered); #12 adds a dedicated regression test confirming both the immediacy and, structurally (an invocation-count assertion, not just timing), that no retry/poll loop runs. See [the design doc](../superpowers/specs/2026-08-16-issue-12-clean-immediate-failure-no-healthy-node-design.md) for the full decision record.
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

Phase 0's core happy path (client → coordinator → node → vLLM → response) is implemented and live-verified as of issue #10 — see success criterion #2 above. #9 already covers success criterion #3 (killing a node removes it from routing) for a single node; #11 ("Coordinator re-routes when the active node goes down") extends that to the multi-node case and remains open (implemented and reviewed as of PR #24, pending live-hardware verification tracked in #25). #12 ("Clean, immediate failure when no healthy node is available") is resolved — success criterion #4's behavior was already built and live-verified under #10; #12 added the regression test that locks it in, with no production code changes needed. All four Phase 0 success criteria now have resolved, tested coverage; #11/#25 are the only piece not yet closed end to end.
```

- [ ] **Step 4: Commit**

```bash
git add docs/phases/phase-0-foundation.md
git commit -m "docs: resolve issue #12's open risk in the phase-0 foundation doc"
```
