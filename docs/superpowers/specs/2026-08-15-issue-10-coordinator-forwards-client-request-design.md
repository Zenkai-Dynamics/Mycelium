# Issue #10 — Coordinator Forwards a Client Request to a Healthy Node — Design

Date: 2026-08-15
Status: Approved, not yet implemented
Issue: [#10 — Coordinator forwards a client request to a healthy node](https://github.com/Zenkai-Dynamics/Mycelium/issues/10)

This is the condensed record of the decisions made while brainstorming/
grilling issue #10, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
the [issue #1](2026-08-12-issue-1-project-skeleton-design.md) through
[issue #9](2026-08-15-issue-9-node-heartbeat-liveness-design.md) design
docs.

## What issue #10 asks for

The core Phase 0 happy path: a client sends a prompt to the coordinator's
one stable address; the coordinator picks a healthy node hosting the
requested model (from the registry built by #8/#9) and forwards the
request to the node agent built by #7; the node's response is returned to
the client unchanged. Acceptance criteria: a client can send a prompt to
the coordinator and receive a correct completion without knowing which
physical node handled it; with exactly one healthy node registered, every
request routes to it correctly. This closes Phase 0 success criterion #2.

Everything this issue needs is already in place except the request path
itself: `server.py`'s post-registration loop (`async for _message in
websocket: pass`) discards every message a node sends after registering,
and `client/cli.py` is a total no-op stub — "Phase 0 skeleton — no
behavior yet."

## Decisions made

**Transport: reuse the existing TLS WebSocket server, not a new HTTP/ASGI
surface.** Issue #8's design doc explicitly deferred this exact call:
"#5's design explicitly deferred introducing an ASGI framework until a
real client-facing route needs it (#10)." That route has arrived, but the
same reasoning still applies — a client request/response is exactly the
same shape as the `status_query` exchange #8 already built (connect, send
one typed JSON message, get one typed JSON response, close), so a second
server stack would add a dependency and a second listener for no
behavioral gain. The client becomes another one-shot `websockets.connect`
caller, symmetric with `mycelium-coordinator-status`.

**Client authentication: the same shared token nodes and
`mycelium-coordinator-status` use.** Matches #8's rationale for reusing the
token for the status query — the operator already personally issued that
token to every participant in this closed pool; a second, client-only
credential is real setup/sync overhead for a node-vs-client distinction
that doesn't otherwise exist yet. An unauthenticated route on a
"publicly-reachable" coordinator (per `phase-0-foundation.md`'s own
description) would let anyone who can reach the port consume GPU time.

**Model selection: the client specifies the model, required.** The
request carries `"model"` explicitly rather than the coordinator silently
routing to "whatever's registered." This matches the phase-0 doc's own
framing ("picks a healthy node hosting the requested model") and means
acceptance criterion 2 is genuinely exercised rather than trivially true
by construction — it also means Phase 2's multi-model routing doesn't need
to add this field back in later.

**Node selection: first registered match, no load balancing.** "Healthy"
means "currently in the registry" — #9 already established that
registration and liveness are the same binary state (a node that goes
silent is dropped within ~50s worst case), so #10 needs no separate
health check of its own. `NodeRegistry.find_node_for_model(model)` scans
the registry and returns the first node whose `model` matches, or `None`.
Phase 0's success bar
wants ≥2 real nodes able to register (they'd all host the same one model
in Phase 0), so more than one candidate is a real possibility even though
"multi-node load balancing" is explicitly out of scope
(`phase-0-foundation.md`) — first-match is the simplest thing that's
still correct, and doesn't pretend to be a fairness algorithm nothing
asked for.

**Failure handling: fail fast with a clear error, no queueing.** If
`find_node_for_model` returns `None`, the coordinator immediately replies
`{"type": "complete_error", "reason": "..."}` — matching
`phase-0-foundation.md`'s explicit "if no healthy node is hosting the
requested model, the coordinator fails the request immediately with a
clear error. No queueing or retry logic in Phase 0."

## Wire protocol

Client → Coordinator:
```json
{"type": "complete", "token": "...", "model": "...", "prompt": "..."}
```
Coordinator → Client, success:
```json
{"type": "complete_result", "text": "..."}
```
Coordinator → Client, failure (no healthy node / node error / node
disconnected / timeout):
```json
{"type": "complete_error", "reason": "..."}
```

Coordinator → Node (over the node's already-open post-registration
connection):
```json
{"type": "complete", "request_id": "<uuid4>", "prompt": "..."}
```
Node → Coordinator:
```json
{"type": "complete_result", "request_id": "...", "text": "..."}
```
or
```json
{"type": "complete_error", "request_id": "...", "reason": "..."}
```

Malformed or incomplete messages (missing `model`/`prompt` on the client
leg, an unparseable/non-dict body) are rejected the same way #8 already
rejects a malformed `register` message — an explicit `complete_error`
reply (client leg) or closing the connection (node leg), never a silent
hang or an unhandled exception.

**Correlation: `request_id` (uuid4) on every coordinator↔node message.**
Concurrent client requests can legitimately land on the same node's single
open connection — without a correlation id, two replies on that connection
would be indistinguishable, which is a real correctness bug, not
speculative robustness. The coordinator keeps `node.pending: dict[str,
asyncio.Future]` (new field on the `Node` dataclass in `registry.py`) and
resolves the matching future when a `complete_result`/`complete_error`
with that `request_id` arrives. Entries are removed from `pending` on
every exit path (success, timeout, disconnect) so the dict never leaks
across a long-lived connection.

**Timeouts, sized so the more specific error wins the race.**
`node/vllm_process.py` already bounds a local completion at
`COMPLETE_TIMEOUT_SECONDS = 120.0`. The coordinator waits up to
`NODE_COMPLETE_TIMEOUT_SECONDS = 130.0` for the node's reply — 10s past the
node's own bound, so if vLLM itself hangs or errors, the node's own
timeout/exception fires first and produces a specific `complete_error`
rather than the coordinator giving up first with a vaguer one. The client
in turn waits up to `CLIENT_COMPLETE_TIMEOUT_SECONDS = 140.0` — 10s past
the coordinator's own bound, for the same reason one layer up.

**`route_request`'s `send()` call is covered by the same failure handling
as the awaited reply.** If the node's connection is already dead, the
`websockets` library can raise `ConnectionClosed` synchronously from
`send()` itself (it checks internal connection state, no network
round-trip needed) — before any future is even registered. Both that case
and "the connection closes while we're waiting for a reply" raise the same
`NodeDisconnectedError`; a client shouldn't be able to (and doesn't need
to) tell "died right before we sent" from "died while we waited" apart.

**Disconnect-while-pending fails fast instead of waiting out the full
timeout.** If a node's connection closes while it has entries in
`node.pending`, `_handle_registration`'s existing `finally:
registry.unregister(...)` cleanup also fails every remaining pending
future for that node with a `NodeDisconnectedError`, so a waiting client
doesn't sit out the full 130s/140s timeout for a node that's already
visibly gone.

**Cleanup targets a captured connection, never a fresh `node_id`
lookup.** Per #8's existing duplicate-`node_id` behavior (a reconnecting
node's new registration replaces the registry entry and closes the old
connection), an old connection can still have its own pending requests in
flight at the moment it's superseded. `_handle_registration`'s task
captures its own `Node` object once, at registration time, and its
cleanup operates on that captured reference directly — never
`registry._nodes[node_id]`, which could by then point at a different,
newer connection under the same id. This falls out naturally from keeping
`pending` on the `Node` object and a local variable in the handler, but is
worth stating explicitly so a future edit doesn't "simplify" it into a
node_id re-lookup and reintroduce the bug.

**Node-side handling: a task per request, no concurrency cap.** The
node's post-registration loop spawns an `asyncio.create_task` per incoming
`"complete"` message, each running `VLLMProcess.complete()` via
`asyncio.to_thread` (matching `cli.py`'s existing pattern for
`wait_ready`/`complete` — the event loop stays free to keep answering
pings and other requests while a thread blocks on the HTTP call to vLLM).
Left uncapped for Phase 0: "no queueing" already rules out serializing
requests behind each other, vLLM does its own internal request batching,
and admission control for a single operator-controlled client is a real
feature nothing in the acceptance criteria asks for. Noted explicitly
below as a Phase 1 candidate rather than silently forgotten.

**`VLLMProcess.complete()` exceptions are caught and reported, not left
to hang or crash the loop.** Matches #8's explicit-ack-or-reject pattern
(`registration_rejected` with a reason) rather than silence: any exception
from the local call to vLLM becomes `{"type": "complete_error",
"request_id": ..., "reason": "..."}` back to the coordinator.

**Client CLI: one-shot, symmetric with `mycelium-coordinator-status`.**
`mycelium-client --coordinator-url ... --coordinator-cert ... --token-file
... --model ... --prompt "..."` connects, sends one `complete`, prints the
result or `error: ...` plus a non-zero exit, and closes — matching the
phase-0 doc's "a basic client interface to send a prompt and get a
completion back" and the acceptance criteria's singular "a client sends
one prompt." `--model` is a required flag with no baked-in default, rather
than importing `node.vllm_process.DEFAULT_MODEL` — the client package
doesn't otherwise depend on the node package, and the model is an
operator-supplied deployment fact, not something to duplicate or
cross-import a default for.

## Module layout

- `mycelium/coordinator/router.py` (new) — `NoHealthyNodeError`,
  `NodeTimeoutError`, `NodeDisconnectedError`; `route_request(node, prompt,
  timeout) -> str`. Mirrors the existing `registry.py`/`server.py` split:
  state/logic in its own module, wire dispatch in `server.py`.
- `mycelium/coordinator/registry.py` — `find_node_for_model(model) -> Node
  | None`; `Node` gains `pending: dict[str, asyncio.Future]`.
- `mycelium/coordinator/server.py` — `_handle_node` gains a `"complete"`
  branch (client requests) alongside the existing `status_query`/
  `register` branches. The post-registration loop in `_handle_registration`
  (currently `async for _message in websocket: pass`) becomes real
  dispatch: `complete_result`/`complete_error` messages resolve the
  matching pending future by `request_id`; its `finally` block additionally
  fails any leftover pending futures for the captured `Node`.
- `mycelium/node/request_handler.py` (new) — loops incoming messages on
  the node's coordinator connection, spawns a task per `"complete"`
  message, calls `VLLMProcess.complete()` via `asyncio.to_thread`, replies.
  Keeps `connection.py` transport-only and `registration.py` scoped to the
  one handshake exchange, matching both modules' existing docstrings.
- `mycelium/node/cli.py` — the post-registration `await
  websocket.wait_closed()` becomes `await
  request_handler.handle_messages(websocket, process)`.
- `mycelium/client/cli.py` — replaces the no-op stub with the one-shot CLI
  described above.

## Testing

Unit tests for `router.py` (node selection incl. no-match, send-time
`ConnectionClosed`, await-time timeout, disconnect-while-pending) and
`request_handler.py` (fake websocket + fake `VLLMProcess`, success and
raised-exception paths), using the simulation techniques already
established in this codebase — `tests/node/fixtures/fake_vllm.py`,
transport pausing, monkeypatched timeouts (`tests/coordinator/
test_server.py`'s existing patterns). An integration test exercises the
full simulated round trip: fake client message → coordinator → fake node
connection → simulated reply → back to the client.

**Live-hardware verification is part of this issue**, matching #4/#6/#7/#8
rather than #9's simulated-only exception — #9's exception applied because
it was proving a `websockets` library guarantee already covered by prior
live runs; #10 is new, Mycelium-specific routing logic never exercised for
real before. Verification: a real `mycelium-client` against a real
coordinator, routed to a real node agent running real `vllm serve` (same
hardware pattern as #7/#8's `a6000` runs), confirming a real prompt
returns a real, correct completion through the full chain.

## Explicitly out of scope for this issue

Multiple simultaneous models or multi-node load balancing beyond
first-match (`phase-0-foundation.md`, unchanged). Queueing, retries, or
failover for a busy/unavailable node. Streaming responses (the node's
`VLLMProcess.complete()` already returns a complete, non-streamed string;
this issue doesn't change that). Any concurrency cap or admission control
on the node's or coordinator's in-flight request handling — noted above as
a real gap Phase 1 may need to close once untrusted clients or nodes are
in the picture, not silently forgotten. Any change to node registration,
heartbeat/liveness detection, or the shared-token auth model (#8/#9,
unchanged). Any new HTTP/ASGI surface on the coordinator.
