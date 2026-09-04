# Phase 2 — Implementation Design

Date: 2026-09-04
Status: Approved, not yet implemented
Related: [Phase 2 design](2026-09-04-phase-2-multi-llm-agentic-design.md) (the *why*),
[Phase 2 doc](../../phases/phase-2-multi-llm-agentic.md) (the *what*),
[PRD: issue #54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54),
[ADR-0003](../../adr/0003-client-side-orchestration.md),
[ADR-0004](../../adr/0004-explicit-per-hop-context.md),
[CONTEXT.md](../../../CONTEXT.md) (glossary)

The Phase 2 design doc settled *what* to build and *why*. This one settles
*how*, for the decisions that span more than one slice — so they get made once
here rather than seven times, implicitly, by whichever ticket happened to land
first. Each sub-issue then gets an implementation plan that cites this
document instead of a near-copy of it.

Everything below was worked out by grilling the PRD and the sub-issues against
the code as it actually stands, not against how the design docs describe it.

## Three things the PRD left genuinely unresolved

**The exposure report needs an identifier the wire doesn't carry.** #59
requires the `Flow` to report per-node *and per-identity* exposure, so
ADR-0004's claim is checkable rather than trusted. `complete_result` carries
`{"type", "text"}` and nothing else — no notion of which node served the hop.
Meanwhile #56 deliberately withholds fingerprints and GitHub logins from
client tokens so a client cannot enumerate volunteers. Those requirements pull
in opposite directions and neither document reconciles them.

The per-identity half is the load-bearing half. The per-identity cap is three,
so "three different nodes saw my hops" can be one person seeing three of my
five hops — which is precisely the aggregation threat ADR-0004 exists to
bound. A per-node-only report would miss it entirely.

**#61 cannot currently meet its own acceptance criterion.** It requires
client↔coordinator overhead reported separately from inference time, and
nothing in the protocol exposes that split — a client knows only total wall
time.

**Normalization had no home.** The design doc claims carrying real roles makes
the node *simpler*. That is only true if the node stops wrapping prompts
itself, which requires naming the component that does.

## Wire protocol

Client → coordinator. `complete` carries `prompt` **xor** `messages`; both set
or neither set is rejected with a specific error rather than a precedence rule.

```
complete:      {type, token, model, messages: [{role, content}, ...]}
               {type, token, model, prompt: "..."}          # shorthand
list_models:   {type, token}

complete_result: {type, text, exposed: [{node_handle, identity_handle}], elapsed_ms}
complete_error:  {type, reason, exposed: [...]}     # exposed empty if nothing was sent
models:          {type, models: [{model, healthy_nodes}]}
```

Coordinator → node carries **only** `messages`. The shorthand dies at the
coordinator.

Node → coordinator: `complete_error` gains `fault: "client" | "node"`. An
absent `fault` reads as `"node"`, so no existing message silently changes
meaning.

`healthy_nodes` means "currently registered, serving that model". The registry
has no other notion of health — a node that stops answering pings is evicted
by the existing keepalive, so registration *is* liveness.

## Decisions

### Exposure identifiers are opaque handles from a per-process secret

The coordinator generates a random secret at startup and returns
`node_handle = HMAC(secret, public_key)` and
`identity_handle = HMAC(secret, identity.id)` with each reply. A client can
group hops by node and by identity — answering "did one volunteer read three
of my five hops?" — without ever learning a fingerprint or a GitHub login.
#56's non-enumeration property survives, and #59's report becomes possible.

The secret is in-memory and per-process, matching how identity bindings, bans
and reputation counters already behave. Handles are therefore not comparable
across a coordinator restart, which costs nothing: a flow lives in one client
process by design. Persisting it would create the first on-disk artifact
capable of linking one client's flows across time.

**Stated plainly, because this project does not overclaim:** handles are
stable for a coordinator's lifetime, so a determined client holding the shared
token could accumulate them and count the network. This is "we do not hand
volunteer identities to clients", not unlinkability. #57 must say so.

### Failed hops report exposure, and failover can expose two nodes

A node that received a `messages` array and *then* failed — vLLM 4xx, timeout,
mid-request crash — has already read it. Handles therefore ride on
`complete_error` as well, and `Flow` counts a failed hop's exposure exactly
like a successful one. Failures where nothing was sent (no healthy node,
coordinator-side validation rejection) carry an empty `exposed`.

`_handle_complete_request` retries a different node on
`NodeDisconnectedError`, and that error covers two different situations that
`router.py` currently declares a caller "shouldn't need to (and can't)"
distinguish. That was true until exposure reporting existed. If the *send*
raised, the node saw nothing; if the connection dropped *while awaiting a
reply*, the node very likely received and processed the request. So
`NodeDisconnectedError` splits into `NodeSendFailedError` and
`NodeDroppedError` — both subclasses, so every existing catch site and test
keeps working — and only the latter counts as exposure. A hop's `exposed` is
consequently a *list*, not a single pair.

This is more machinery than "simplicity first" would reach for by default. It
earns its place because under-reporting exposure is a false privacy claim, the
router already knows which branch fired, and the alternative — listing every
node attempted — is also a false claim, merely in the flattering direction.

One residual inaccuracy is worth recording rather than papering over: a send
that succeeds locally but dies before delivery would be counted as exposure
that never happened. The report errs toward over-reporting in that narrow
case, which is the right direction for a privacy report to err.

### The coordinator normalizes `prompt` into `messages`

The client-facing wire accepts either; the coordinator rejects both-set and
neither-set, expands `prompt` to a one-message array, and forwards only
`messages`. The node deletes its wrapping and passes the array straight to
vLLM — genuinely simpler, as the design doc claims. One validation point, in
the component that already authenticates the request.

This changes the node-facing wire. That is fine: nodes are versioned with the
coordinator and registration has never negotiated a version.

### `messages` validation is structural only

Non-empty list of dicts, each with a string `role` and a string `content`.
Nothing else. Forwarding unvalidated content would burn a volunteer's GPU on a
request we could reject for free; restricting `role` to system/user/assistant
would hard-code a vocabulary chat templates already exceed — `tool` being the
obvious case — making Mycelium the component that breaks a legitimate request.

### Client-caused faults are flagged by the node, and 404 is not one

`vllm_process.complete` raises `VLLMClientError` (4xx other than 404) or
`VLLMServerError` (404, 5xx, transport failures — see the 404 carve-out
below), carrying vLLM's own message read from the response
body rather than a bare `HTTP Error 400: Bad Request`. `request_handler` maps
those to `fault` and stops needing to know `urllib` exists — keeping the HTTP
boundary owned by the module that owns the HTTP call, the same separation
`vllm_process` already maintains by knowing nothing about the coordinator
connection.

`route_request` raises `ClientRequestError` — a `RoutingError` that is
deliberately *not* a `NodeError` — when a reply carries `fault: "client"`. The
server relays it without touching reputation and without failing over to
another node: the request is bad, so a second volunteer would only fail
identically.

**404 is classified as a node fault, not a client fault.** vLLM returns 404
when asked for a model it is not serving, which means the node registered
`--model X` while serving Y. Treating that as the client's fault would
permanently shield a misconfigured node from reputation damage — #58
overshooting into the mirror image of the bug it exists to fix. It is the only
4xx that is structurally about the node rather than the request.

The node never truncates to make a request fit. Overflow returns vLLM's own
message, which names the limit and the requested size.

### Latency: the coordinator reports `elapsed_ms`

The coordinator times its own `route_request` and returns `elapsed_ms`; the
client computes overhead as `wall_time − elapsed_ms`. That isolates exactly
the leg ADR-0003 put in question — the (N−1) extra client↔coordinator
round-trips, TLS handshake included.

The field is coordinator-side wire work, so it lands with the exposure-handle
slice below rather than with #61 — live-verification tickets in this repo
record results; they do not add protocol. `Flow` consumes it in #59.

### Each hop keeps its own connection

Unchanged: the coordinator closes the socket after one completion, so an
N-hop flow costs N handshakes. Connection reuse before measurement would be
optimizing against the exact assumption this phase exists to test. If #61's
numbers show handshakes dominate, reuse becomes a follow-up ticket with data
behind it — additive, since the coordinator would stay conversation-stateless
either way.

### The Flow API

```python
flow = Flow(coordinator_url, cert, token)

draft  = await flow.call("small-model", messages=[user(task)])
review = await flow.call("big-model",   messages=[user(critique),
                                                  assistant(draft.text)])

flow.hops        # Hop(index, model, sent, text, exposed, elapsed_ms, wall_ms, error)
flow.exposure()  # handle -> hop indices
await flow.list_models()
```

`call` is the single primitive. A failed hop is appended to the record and
*then* raises `HopError`, so the record stays intact and usable — as #59
requires — while the agent still cannot ignore the failure. Raising matches
the codebase, which raises `CompletionError`, `QueryError` and `RoutingError`
throughout.

Module-level `system()`, `user()` and `assistant()` helpers ship with it: three
one-liners that make ADR-0004's documented example literally runnable rather
than pseudocode. `Flow.list_models()` is on the surface because an agent should
be able to check what is served before committing to a hop. There is
deliberately **no** `prompt=` shorthand on `call` — one way to pass content
keeps the explicit-context rule visually obvious at every call site.

Concurrent calls are allowed; `asyncio.gather` across models is a plausible
agent shape and each hop has its own connection anyway. A hop's index is
assigned when the call *starts*, not when it completes, so the record's
numbering is deterministic and matches the agent's call order rather than
whichever model answered first.

`exposure()` returns data — handles mapped to hop indices. Formatting is the
example's job. The library stays a primitive; #61 asserts against structure
rather than parsing text.

### Transport extraction happens in #59, client-side only

`status_cli`, `ban_cli` and `client/cli` each hand-roll connect/send/receive
today. That duplication stands until `Flow` arrives as the third client-side
consumer, at which point a small `client/transport.py` is extracted and shared
by `Flow` and the client CLIs. `Flow` needs the round-trip wrapped anyway for
timing and handle extraction, so the extraction pays for itself instead of
being speculative. The operator CLIs are not touched — this phase has no
business editing them.

### The reference agent sends the task, and says why

ADR-0004's sample shows a critique hop receiving `[user(critique),
assistant(draft)]` and explicitly *not* the original task. A critic that cannot
see the task cannot judge whether the draft answers it, so the reference agent
passes the task — and prints that reasoning next to what the hop actually
sent.

That is the honest demonstration. Explicit context does not mean "send less";
it means "decide per hop, and be able to see what you decided". The shipped
example's own exposure report will show the second node seeing the task, which
is the point rather than a flaw.

ADR-0004 gets a sentence in **Consequences** recording this, so a reader who
notices the demo doing what the sample says not to can tell which is
authoritative. The decision itself — no implicit accumulator — is untouched.

### Client-facing CLI shape

`mycelium-client --messages-file conversation.json`, mutually exclusive with
`--prompt`. A JSON file rather than repeatable `--message role:content` flags,
because message content routinely contains colons, quotes and newlines. This
gives #55 an end-to-end surface to document in `OPERATIONS.md` before the
library exists, and gives #61 a way to test multi-turn before the agent does.

`mycelium-client-models` is a separate entry point with flat argparse,
matching the `mycelium-coordinator-status` / `mycelium-coordinator-ban`
precedent rather than introducing subcommands.

## Work breakdown

Eight slices. The eighth is new: the exposure-handle machinery is
coordinator, router and node-wire work that exists only to serve #59's report,
and is independently testable server-side without any client library. Splitting
it keeps #59's diff inside the client package rather than spanning every
component in the system.

| Slice | Owns | Blocked by |
|---|---|---|
| #55 | `prompt`/`messages` wire, coordinator normalization + validation, node takes `messages`, `--messages-file` | — |
| #56 | `list_models`, `mycelium-client-models` | — |
| #58 | typed vLLM errors, `fault` field, `ClientRequestError`, 404 carve-out, reputation fix | — |
| *new* | coordinator secret, HMAC handles, `exposed` on both replies, `NodeDisconnectedError` split, `elapsed_ms` | — |
| #59 | `client/transport.py`, `Flow`/`Hop`/helpers, `exposure()`, `list_models()` | #55, new |
| #57 | privacy docs incl. handle limits; ADR-0004 Consequences note | — |
| #60 | reference agent in `examples/` | #59 |
| #61 | live verification and latency measurement | #60 |

`CONTEXT.md` gains **handle** as a term when the new slice lands — an opaque
per-coordinator-process identifier for a node or an identity, which a client
can group by but cannot resolve.

## Testing

Follows the existing suite: real local websocket connections, fake external
dependencies, no real network calls to GitHub or real vLLM in unit tests.

- `tests/node/fixtures/fake_vllm.py` gains the ability to return a chosen
  status — 400, 404 and 500 — so #58's classification is tested against real
  HTTP behavior rather than a mocked exception.
- #59's "nothing is sent that the call did not name" is verified by capturing
  the raw frames a fake coordinator received and asserting on the exact JSON,
  not by inspecting the library's internal state.
- Exposure accuracy is verified by comparing `flow.exposure()` against what
  the fake nodes actually received, including a failover case where a node
  receives a request and then drops.
- Reputation is asserted unchanged after a client-caused fault, and unchanged
  in the same way after a 404 is *not* treated as one.

## Out of scope for implementation

Connection reuse across hops (revisit only if #61's numbers demand it).
Persisting the handle secret. Per-model reputation — reputation is keyed by
node public key and a node serves one model, so it is already
per-(node, model). Retrying or substituting models on a failed hop. Everything
the Phase 2 design doc already excluded: streaming, conversation persistence,
capability-based routing, multi-model-per-node, public client access, and all
of Phase 3.
