# Issue #71 — Relay a Node's Fault Through the Coordinator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `fault: "node"` actually reach the client when a node crashes or times out, instead of being silently dropped by the coordinator — and correct the documentation/comments elsewhere in the repo that describe this as an open gap.

**Architecture:** `node/request_handler.py` already sends `fault: "node"` correctly. `coordinator/server.py`'s `_handle_complete_request` discards it in two of its exception handlers by calling `reject(str(exc))` with no `fault` argument. The fix is to pass the literal `fault="node"` at those two call sites — `NodeError` and `NodeTimeoutError` already mean "the node's own fault" by construction, so there is no value to derive, only a constant to pass. `NoHealthyNodeError` (the generic `RoutingError` catch-all) deliberately keeps no fault, per this project's existing #58 decision, re-confirmed for this issue.

**Tech Stack:** Python, `pytest` (`asyncio_mode = "auto"`, so `async def test_...` needs no decorator), `websockets`.

## Global Constraints

- The client-facing `reason` string must never name a node (issue #63) — untouched by this plan, but do not regress it.
- `fault` values are exactly `"client"`, `"node"`, or the field is absent. Do not invent a third value (this project's own #58 decision, reconfirmed for #71: "no healthy node" stays faultless).
- Don't touch reputation counters, exposure semantics, or the retry loop's control flow — only the `fault` argument passed to `reject()`.
- Dated files under `docs/superpowers/specs/` are historical records and are never edited after the fact — do not touch `docs/superpowers/specs/2026-09-05-issue-60-reference-agent-design.md` even though it references #71.
- Match existing code style exactly (docstring tone, comment density) — this codebase writes dense, reasoned comments explaining *why*, not *what*.

---

### Task 1: Coordinator relays `fault: "node"` on crash and timeout

**Files:**
- Modify: `src/mycelium/coordinator/server.py:140-148` (docstring), `:234-238` (`NodeTimeoutError` handler), `:239-243` (`NodeError` handler)
- Test: `tests/coordinator/test_server.py` — modify `test_node_reported_failure_still_reports_that_node_as_exposed` (currently ends around line 2019) and `test_complete_request_timeout_increments_timeout_counter` (currently ends around line 1355)

**Interfaces:**
- Consumes: `router.NodeTimeoutError`, `router.NodeError` (both already exist, unchanged), the existing `reject(reason: str, fault: str | None = None)` closure inside `_handle_complete_request` (unchanged signature).
- Produces: nothing new is exposed to later tasks — this task changes coordinator *behavior* (an extra key in an existing reply dict), which Task 2 relies on only to explain in comments, not to call directly.

- [ ] **Step 1: Update the crash test to expect the fault it should have had all along**

In `tests/coordinator/test_server.py`, find `test_node_reported_failure_still_reports_that_node_as_exposed`. It currently ends:

```python
    assert response["type"] == "complete_error"
    assert response["exposed"][0]["node_handle"] == crypto.handle(b"s" * 32, public_key)
    assert isinstance(response["elapsed_ms"], int)
    # Exactly these keys and nothing else, same reasoning as the
    # complete_result case above.
    assert set(response) == {"type", "reason", "exposed", "elapsed_ms"}
    assert set(response["exposed"][0]) == {"node_handle", "identity_handle"}
```

Change it to:

```python
    assert response["type"] == "complete_error"
    assert response["fault"] == "node"
    assert response["exposed"][0]["node_handle"] == crypto.handle(b"s" * 32, public_key)
    assert isinstance(response["elapsed_ms"], int)
    # Exactly these keys and nothing else, same reasoning as the
    # complete_result case above.
    assert set(response) == {"type", "reason", "fault", "exposed", "elapsed_ms"}
    assert set(response["exposed"][0]) == {"node_handle", "identity_handle"}
```

- [ ] **Step 2: Update the timeout test to capture and check the client's reply**

In the same file, find `test_complete_request_timeout_increments_timeout_counter`. It currently sends the client request and discards the reply:

```python
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                await client_ws.recv()
```

Change it to capture and assert on the reply:

```python
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())
                assert response["fault"] == "node"
```

- [ ] **Step 3: Run both tests and confirm they fail against today's code**

Run: `pytest tests/coordinator/test_server.py -k "test_node_reported_failure_still_reports_that_node_as_exposed or test_complete_request_timeout_increments_timeout_counter" -v`

Expected: both FAIL — the crash test on `KeyError`-style assertion (`response["fault"]` missing, so `response["fault"] == "node"` raises `KeyError`), the timeout test the same way. This confirms the gap the issue describes actually exists before you fix it.

- [ ] **Step 4: Pass `fault="node"` at both catch sites in server.py**

In `src/mycelium/coordinator/server.py`, `_handle_complete_request` currently has:

```python
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
```

Change both `reject` calls:

```python
        except router.NodeTimeoutError as exc:
            exposed.append(registry.handles_for(node.public_key))
            registry.record_timeout(node.public_key)
            await reject(str(exc), fault="node")
            return
        except router.NodeError as exc:
            exposed.append(registry.handles_for(node.public_key))
            registry.record_crash(node.public_key)
            await reject(str(exc), fault="node")
            return
```

- [ ] **Step 5: Update `_handle_complete_request`'s docstring to describe the new `fault` value**

The docstring currently ends:

```python
    A node-reported failure now further separates whose fault it was —
    see the design doc for issue #58. A `router.ClientRequestError`
    (the request itself was bad, e.g. context too long) counts as
    exposure like any other failure but is recorded against nobody's
    reputation and is not retried on another node; a plain
    `router.NodeError` still records a crash exactly as before. The
    reply's `fault` field ("client" or absent) tells the caller the
    same thing, whether the rejection came from vLLM via the node or
    from this coordinator's own request validation."""
```

Change the last two sentences:

```python
    A node-reported failure now further separates whose fault it was —
    see the design doc for issue #58. A `router.ClientRequestError`
    (the request itself was bad, e.g. context too long) counts as
    exposure like any other failure but is recorded against nobody's
    reputation and is not retried on another node; a plain
    `router.NodeError` still records a crash exactly as before. The
    reply's `fault` field ("client", "node", or absent) tells the
    caller whether the rejection came from vLLM via the node, from
    this coordinator's own request validation, or from the node
    itself crashing or timing out on its own account — see the design
    doc for issue #71. Absent means neither: no healthy node was
    available for the model at all."""
```

- [ ] **Step 6: Run the two updated tests again and confirm they pass**

Run: `pytest tests/coordinator/test_server.py -k "test_node_reported_failure_still_reports_that_node_as_exposed or test_complete_request_timeout_increments_timeout_counter" -v`

Expected: both PASS.

- [ ] **Step 7: Run the full coordinator test file to confirm no regressions**

Run: `pytest tests/coordinator/test_server.py -v`

Expected: all PASS, including `test_no_healthy_node_carries_no_fault` (must still pass unchanged — this is the case that deliberately keeps no `fault`) and `test_client_fault_is_relayed_with_its_fault_and_exposure` (must still pass unchanged — `fault: "client"` path is untouched by this task).

- [ ] **Step 8: Run the full test suite**

Run: `pytest`

Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add src/mycelium/coordinator/server.py tests/coordinator/test_server.py
git commit -m "$(cat <<'EOF'
fix: relay a node's own fault through the coordinator (#71)

A node crash or timeout already arrived at request_handler.py tagged
fault: "node", but _handle_complete_request's reject() calls for
NodeTimeoutError and NodeError never passed it on, so the client could
not tell "the volunteer's machine failed" from "no reply arrived at
all". No healthy node stays faultless per the existing #58 decision.
EOF
)"
```

---

### Task 2: Correct the reference agent's comments and add coverage for the newly-reachable "node" fault branch

**Files:**
- Modify: `examples/draft_then_critique.py:129-156` (`_print_exposure` docstring), `:164-180` (`if who_served_unknown:` comment), `:195-218` (`main()`'s `except HopError` block comment)
- Modify: `src/mycelium/client/flow.py:63-69` (`Hop`'s `elapsed_ms` docstring sentence)
- Modify: `tests/examples/test_draft_then_critique.py` — add a new test; update the docstring of `test_main_does_not_call_a_full_reply_a_floor` (currently around line 332)

**Interfaces:**
- Consumes: Task 1's coordinator behavior (a real coordinator now sends `fault: "node"` for crash/timeout) — but the new test in this task talks to a fake coordinator that crafts its own reply JSON directly, so it does not exercise Task 1's code and has no runtime dependency on it.
- Produces: nothing consumed by anything later — this task is documentation/comment correctness plus one new regression test.

- [ ] **Step 1: Add a test for the previously-dead `elif exc.fault == "node":` branch**

This branch already exists in `examples/draft_then_critique.py`'s `main()` — nothing in this step changes that file's logic. It has simply never been reachable against a real coordinator (Task 1 fixes that) and has never been tested at all (confirmed by grep — no existing test references `fault == "node"` or the printed string below).

In `tests/examples/test_draft_then_critique.py`, add this test after `test_main_reports_a_failed_hop_without_a_traceback` (which ends around line 284):

```python
async def test_main_reports_a_node_fault_distinctly_from_unattributed(tmp_path, capsys):
    """Exercises `main()`'s `elif exc.fault == "node":` branch. It already
    existed in the source but nothing before #71 could ever trigger it
    against a real coordinator, and nothing tested it. This test talks to
    a fake coordinator that sends `fault: "node"` directly, so it pins the
    example's own branching logic independently of the coordinator fix."""
    agent = _load_example()
    fake = _FakeCoordinator(_queued([{
        "type": "complete_error", "reason": "vLLM exploded",
        "fault": "node",
        "exposed": [{"node_handle": "a3f9c2e1b4d6f8a0", "identity_handle": "7b1d4408c2e6f1a3"}],
        "elapsed_ms": 118,
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
    assert "vLLM exploded" in out
    assert "  fault: node — the volunteer's machine failed, not your request" in out
    assert "node a3f9c2e1b4d6f8a0 saw hops [0]" in out
    assert "floor" not in out
```

- [ ] **Step 2: Run the new test and confirm it passes immediately**

Run: `pytest tests/examples/test_draft_then_critique.py -k test_main_reports_a_node_fault_distinctly_from_unattributed -v`

Expected: PASS. This is expected to pass without any production-code change in this task — it is closing a test-coverage gap on existing logic, not driving new logic. If it fails, the branch's print string in `examples/draft_then_critique.py` does not match the assertion byte-for-byte (check the em-dash character `—`) — fix the assertion to match the source, not the other way around.

- [ ] **Step 3: Correct the stale forward-reference in `main()`'s `except HopError` block**

In `examples/draft_then_critique.py`, the `else` branch currently reads:

```python
        else:
            # No `fault` field came back. Today that covers two unrelated
            # situations, which is why this says nothing about whether a
            # node was reached: a transport failure where no reply arrived
            # at all, and *every* node-side failure — crash, timeout, no
            # healthy node — because the coordinator's reject() only ever
            # sets `fault` for client faults. Issue #71 tracks giving
            # node-side failures their "node" fault; when it lands, the
            # second group moves to the branch above and this one narrows
            # to genuine transport failures. The exposure section below
            # does not resolve the ambiguity either, and does not try to:
            # its caveat is about whether this client learned *who* served
            # the hop, which is a different question from whether a reply
            # arrived. A no-healthy-node reply arrives and still names
            # nobody, so it prints a floor. The three paths that reach
            # that caveat are indistinguishable from a Hop today — see
            # _print_exposure — and #71 is what would separate them.
            print("  fault: unattributed — nothing came back saying whose failure this was")
```

Replace the comment (the `print` line is unchanged) with:

```python
        else:
            # No `fault` field came back. Since #71, that no longer
            # covers node-side failures: crash and timeout both arrive
            # tagged `fault: "node"` now, handled in the branch above.
            # What remains here is narrower but still two situations: a
            # transport failure where no reply arrived at all, and a
            # `no healthy node for model X` reply, which the coordinator
            # deliberately still sends faultless (#58: it is neither the
            # client's mistake nor any node's). The exposure section
            # below does not resolve that remaining ambiguity either,
            # and does not try to: its caveat is about whether this
            # client learned *who* served the hop, a different question
            # from whether a reply arrived. A no-healthy-node reply
            # arrives and still names nobody, so it prints a floor. The
            # two paths that reach that caveat are indistinguishable
            # from a Hop alone — see _print_exposure.
            print("  fault: unattributed — nothing came back saying whose failure this was")
```

- [ ] **Step 4: Correct `_print_exposure`'s docstring**

Currently:

```python
def _print_exposure(flow: Flow, who_served_unknown: bool) -> None:
    """Print who saw what, and say so honestly when the client cannot know.

    The caveat is keyed off `elapsed_ms`, not off `fault`. `fault` answers
    a different question entirely (whose mistake it was), and keying on it
    made this example contradict itself: it printed "the coordinator may
    already have sent your content to a volunteer" three lines after
    naming the volunteer that saw it, because a node crash comes back with
    a populated `exposed` and no `fault` at all.

    `elapsed_ms` is the coordinator's measure of its own routing time, and
    its absence means the coordinator never timed a routing attempt for
    this hop. That covers three situations the client cannot tell apart:
    a refused or unreachable coordinator (nothing sent, true count zero),
    a reply saying no healthy node was available (nothing routed, true
    count zero), and a round trip that sent everything and got no reply
    back (true count unknown, possibly more than zero).

    So the note below says only what holds across all three — that this
    client did not learn who served the hop, and the figures are a floor.
    It is deliberately weaker than the code could be: "no reply arrived"
    would be false on the no-healthy-node path, where a full reply did
    arrive. Distinguishing them from the Hop alone is impossible today
    — all three give `elapsed_ms=None`, `exposed=[]` and `fault=None`, and
    only the reason string differs, which this project does not match on
    across a component boundary. Issue #71 is where the coordinator-side
    signal that would make this exact belongs.
    """
```

Replace only the final paragraph (everything before it is still accurate and unchanged by #71 — a node crash/timeout was never one of the three `elapsed_ms=None` situations, because both already carry a populated `elapsed_ms`):

```python
    So the note below says only what holds across all three — that this
    client did not learn who served the hop, and the figures are a floor.
    It is deliberately weaker than the code could be: "no reply arrived"
    would be false on the no-healthy-node path, where a full reply did
    arrive. Distinguishing them from the Hop alone is impossible today,
    and stays that way after #71: that issue relays `fault: "node"` for
    a node's own crash or timeout, and both already carried a populated
    `elapsed_ms` — neither was ever one of these three situations. They
    remain indistinguishable on purpose: #58 decided a no-healthy-node
    reply stays faultless, and a client-side transport failure has no
    coordinator-side signal to relay in the first place.
    """
```

- [ ] **Step 5: Correct the `if who_served_unknown:` comment**

Currently:

```python
    if who_served_unknown:
        # Claims neither that a reply arrived nor that one didn't, and
        # neither that the content was seen nor that it wasn't. All four
        # are possible on the paths this fires on (see above); a floor is
        # the strongest statement true on every one of them.
        #
        # The design doc for issue #59 is the reference for why exposure
        # is a floor. The flow library's Hop docstring covers the same
        # ground for the transport-failure case, but it is not the
        # authority for the condition tested here: it says `elapsed_ms` is
        # None "whenever no reply arrived to carry it", which the
        # no-healthy-node path disproves. That wording is #71's to fix —
        # it is under src/, and #60 does not touch the library.
        print(
            "\n  note: this client never learned who — if anyone — served "
            "that hop, so read these figures as a floor rather than a count."
        )
```

Replace the second comment paragraph (the first paragraph and the `print` are unchanged):

```python
    if who_served_unknown:
        # Claims neither that a reply arrived nor that one didn't, and
        # neither that the content was seen nor that it wasn't. All four
        # are possible on the paths this fires on (see above); a floor is
        # the strongest statement true on every one of them.
        #
        # The design doc for issue #59 is the reference for why exposure
        # is a floor. The flow library's Hop docstring covers the same
        # ground for the transport-failure case; #71 corrected its
        # `elapsed_ms` wording to also name the no-healthy-node path
        # explicitly, rather than leaving the mismatch this comment used
        # to flag.
        print(
            "\n  note: this client never learned who — if anyone — served "
            "that hop, so read these figures as a floor rather than a count."
        )
```

- [ ] **Step 6: Correct `Hop`'s `elapsed_ms` docstring sentence in `flow.py`**

In `src/mycelium/client/flow.py`, the `Hop` docstring currently reads:

```python
    `elapsed_ms` is the coordinator's own measure of routing time (issue
    #63) and is None whenever no reply arrived to carry it — which is not
    the same as no node having been attempted, see below; `wall_ms` is the
    whole round trip as the client experienced it. Their difference is the
    client-coordinator overhead issue #61 measures.
```

Change the second clause to name both cases explicitly:

```python
    `elapsed_ms` is the coordinator's own measure of routing time (issue
    #63) and is None whenever the coordinator's reply carried no such
    value — either no reply arrived at all, or one arrived before any
    node was ever attempted (a `no healthy node` rejection). It is not the
    same as no node having been attempted for every failure, see below: a
    node that was tried and then crashed or timed out (issue #71) still
    leaves `elapsed_ms` populated; `wall_ms` is the whole round trip as
    the client experienced it. Their difference is the client-coordinator
    overhead issue #61 measures.
```

- [ ] **Step 7: Correct the docstring of `test_main_does_not_call_a_full_reply_a_floor`**

In `tests/examples/test_draft_then_critique.py`, this test currently reads:

```python
async def test_main_does_not_call_a_full_reply_a_floor(tmp_path, capsys):
    """A node-side failure comes back as a complete_error carrying no
    `fault` field but a populated `exposed` — the coordinator's reject()
    only ever sets `fault` for client faults (issue #71), while every
    reply names who saw the content.

    That reply arrived. The client knows exactly who saw the hop, so the
    figures are a count. Keying the caveat off `fault is None` made the
    example print "these figures are a floor" three lines after naming
    the node that saw the content, and "the request never reached a node"
    about a request a node had demonstrably read.
    """
```

Change only the docstring (the test body and its fake reply are unchanged — this exact wire shape, populated `exposed` with no `fault`, is still reachable after #71: a node drops mid-request, gets counted as exposure, and the retry loop then finds no more healthy nodes, which stays faultless):

```python
async def test_main_does_not_call_a_full_reply_a_floor(tmp_path, capsys):
    """A `no healthy node` reply that arrives after an earlier node in the
    same request already dropped mid-flight: `exposed` is populated (that
    node read the content) but the coordinator's reject() still sends no
    `fault`, on purpose (#58: no healthy node belongs to neither party) —
    even though #71 made it set `fault: "node"` for the node's own crash
    or timeout. Every reply, faulted or not, names who saw the content.

    That reply arrived. The client knows exactly who saw the hop, so the
    figures are a count. Keying the caveat off `fault is None` made the
    example print "these figures are a floor" three lines after naming
    the node that saw the content, and "the request never reached a node"
    about a request a node had demonstrably read.
    """
```

- [ ] **Step 8: Run the examples and client test files**

Run: `pytest tests/examples/test_draft_then_critique.py tests/client/test_flow.py -v`

Expected: all PASS, including the new test from Step 1.

- [ ] **Step 9: Run the full test suite**

Run: `pytest`

Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add examples/draft_then_critique.py src/mycelium/client/flow.py tests/examples/test_draft_then_critique.py
git commit -m "$(cat <<'EOF'
docs: the fault-relay gap #71 fixed is no longer open (#71)

The reference agent's comments and Hop's elapsed_ms docstring described
node crash/timeout as indistinguishable from "no reply arrived" and
pinned the fix to #71 by name. Now that #71 relays fault: "node",
correct those comments instead of leaving a stale forward-reference,
and add the test coverage main()'s "node" fault branch never had.
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- "A client receives `fault: "node"` when a node crashes, times out, or otherwise fails on its own account" → Task 1, Steps 4/6.
- "A client can distinguish a node-attributed failure from 'no reply arrived at all'" → Task 1 (the reply now differs: `fault: "node"` present vs. absent).
- "The client-facing `reason` still never names a node (#63)" → unchanged by this plan; `reject(str(exc), fault="node")` still passes only `str(exc)`, whose anonymity #63 already guaranteed (`NodeTimeoutError`'s message is deliberately anonymous per router.py's own comment).
- "`docs/OPERATIONS.md`'s documented `fault == "node"` branch becomes reachable" → true once Task 1 lands; confirmed during design that no wording there needs changing (it already reads as live).
- "#60's reference agent is revisited: its floor-not-count caveat currently works around this gap and can be made exact" → Task 2, Steps 3-7 (the caveat is corrected to state precisely what remains ambiguous and why, rather than promising a fix this plan's confirmed scope doesn't deliver).

**Placeholder scan:** No TBD/TODO; every step shows exact before/after code or the exact new test body.

**Type consistency:** `reject`'s signature (`reason: str, fault: str | None = None`) is unchanged; both new call sites use it exactly as `ClientRequestError`'s existing call already does two branches up in the same function.
