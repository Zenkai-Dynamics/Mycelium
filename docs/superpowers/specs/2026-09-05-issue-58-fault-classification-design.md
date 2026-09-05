# Issue #58 — Client-Caused Faults Must Not Damage Node Reputation — Design

Date: 2026-09-05
Status: Approved, not yet implemented
Related: [Phase 2 implementation design](2026-09-04-phase-2-implementation-design.md),
[issue #63's design](2026-09-05-issue-63-exposure-handles-design.md) (the retry loop this builds on),
[issue #58](https://github.com/Zenkai-Dynamics/Mycelium/issues/58),
[PRD: issue #54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54)

The cross-slice Phase 2 design settled the shape: the node reports whether a
failure was the client's fault, and only genuine node faults touch reputation.
This is the slice-level record of the decisions that only surfaced with the
code in front of us — chiefly that the node making the claim is an untrusted
volunteer with an incentive to lie.

## The defect

A client sends more context than the model's window holds. vLLM returns HTTP
400. `urllib` raises. `request_handler`'s broad `except Exception` turns it
into a `complete_error`. The coordinator maps that to `NodeError` and calls
`registry.record_crash(...)`.

**A client's own mistake permanently damages an innocent volunteer's
reputation**, and reputation-weighted routing then sends that volunteer less
work. The bug exists today; Phase 2 makes it far easier to hit, because
context grows every hop by design.

## Decisions

### The node self-reports fault, and the node is untrusted

This is the decision the earlier design did not confront. `fault` is asserted
by a volunteer's machine, and Phase 1 opened the pool to the public. A node
that always claims `"client"` never accrues crashes, keeps a
`_reputation_weight` near 1.0, and wins routing over honest nodes. There is no
coordinator-side check that could verify the claim: it never sees vLLM's
request or response.

**A separate `client_faults` counter records how often each node made the
claim.** It does *not* feed `_reputation_weight` — that would recreate the
exact bug being fixed — but it appears in `list_nodes()` and in
`mycelium-coordinator-status`, so a node claiming it constantly is visible
rather than invisible. One integer converts an unverifiable assertion into an
observable one.

Coordinator-side discounting (honour only N claims per window) was rejected:
any threshold is arbitrary, and it would penalize a node genuinely serving a
client that keeps overflowing the window.

Worth stating plainly: this does not *stop* a dishonest node, it only shows
it. That is proportionate. Reputation measures protocol-level reliability
only, never output quality — a malicious volunteer can already return
confident garbage and lose nothing.

### Anything not exactly `"client"` is a node fault

An omitted `fault` means an older node build, whose failures are node faults
exactly as they are today, so nothing silently changes meaning. An
unrecognized value means a broken or hostile node, and defaulting it to
`"node"` denies a node the option of dodging reputation by sending garbage
instead of a valid claim. The permissive direction is the exploitable one.

### 401, 403, 404, 408, 413 and 429 are node faults despite being 4xx

The cross-slice design recorded "4xx except 404". More carve-outs:

| Code | Fault | Why |
|---|---|---|
| 401 | node | the node's own auth to vLLM is misconfigured |
| 403 | node | the node's own auth to vLLM is misconfigured |
| 404 | node | vLLM isn't serving the model the node registered |
| 408 | node | the node timed out serving |
| 413 | node | the node's own proxy/config rejected the request's size |
| 429 | node | the node is overloaded |
| other 4xx | client | the request itself was not acceptable |
| 5xx, transport | node | the node broke |

All six exceptions are structurally about the volunteer's capacity or
configuration rather than about the request. Classifying them as client faults
would shield a node that genuinely cannot serve from ever reflecting that —
the same overshoot 404 was carved out to avoid, which is the argument for
extending it rather than a new one.

vLLM does not rate-limit by default, so 429 may never appear in practice; nor
does it require auth or enforce a request-size limit by default, so 401, 403
and 413 may never appear either — in the shipped configuration vLLM is
launched by the node itself, bound to loopback, with no api-key, so none of
the five are reachable today. The carve-out costs one tuple entry each and
covers a node sitting behind a proxy that does add one of these.

### A client fault still counts as exposure

The node received the conversation and handed it to vLLM, which had to read
and tokenize it to discover the overflow. Under [#63](2026-09-05-issue-63-exposure-handles-design.md)'s
rule — the bytes left the coordinator for this node — the hop is exposure and
appends to `exposed` like any other outcome. Omitting it would under-report,
the direction #63's fix wave existed to close.

No document covered this interaction before, because the two slices were
designed apart.

### The client is told whose fault it was

`complete_error` carries `fault` through to the client, not just to the
coordinator. #59's `Flow` has to decide what a failed hop means: a client
fault should be fixed by trimming context, a node fault might be retried
elsewhere. Without the field an agent can only pattern-match the reason text —
the string-sniffing across a component boundary this project already rejected
once. It discloses nothing about the volunteer: it says whose mistake it was,
not who served it.

The coordinator's own validation rejections carry `fault: "client"` too — a
malformed request is the client's mistake whether vLLM or the coordinator
caught it, and an agent should not need two code paths for one class of error.
`no healthy node for model X` carries no `fault` at all: it is neither the
client's mistake nor any node's, and inventing a third value for one case is
vocabulary nobody asked for. This follows #63's precedent that a field appears
only where it is meaningful.

### Fault classification lives in `vllm_process`

`vllm_process.complete` raises `VLLMClientError` or `VLLMServerError`,
carrying vLLM's own message — parsed from the JSON error body, falling back to
the raw body when it will not parse or lacks the field, so an unexpected error
shape still yields something actionable rather than an empty string. That
message is what makes an overflow error useful: it names the limit and the
size requested.

`request_handler` maps those to `fault` and stops needing to know `urllib`
exists — keeping the HTTP boundary owned by the module that owns the HTTP
call, the same separation `vllm_process` already maintains by knowing nothing
about the coordinator connection.

`router.ClientRequestError` is a `RoutingError` and deliberately **not** a
`NodeError`, so no existing `except NodeError` site can accidentally record a
crash for it. In `_handle_complete_request` its branch sits after `NodeError`
and before `RoutingError`, appends exposure, records the counter, and returns
without failing over — the request is bad, so a second volunteer would fail
identically.

### Scope

`CONTEXT.md` gains **Fault**: whose mistake a failure was, deliberately not
the same as who failed, and explicitly not a judgement about output quality.
The glossary exists because these words are overloaded, and this is the new
overloaded one.

No client CLI change. `Flow` consumes `fault` in #59, matching how #63 left
its wire fields unconsumed until the library arrived.

## Sequencing

Built on `main` after #63, which restructured the same retry loop. Doing it
second means this slice adds one branch to a loop whose shape has settled,
rather than two slices rewriting the same forty lines from different starting
points.

## Out of scope

Making a node's fault claim verifiable — the coordinator cannot see vLLM's
exchange, and proposals that would require it belong to a different phase.
Any change to reputation *weighting*. Client-side consumption. Everything the
Phase 2 implementation design already excluded.
