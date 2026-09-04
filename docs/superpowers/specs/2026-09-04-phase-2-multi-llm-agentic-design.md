# Phase 2 — Multi-LLM Agentic Flow — Design

Date: 2026-09-04
Status: Approved, not yet implemented
Related: [ADR-0003 — client-side orchestration](../../adr/0003-client-side-orchestration.md),
[CONTEXT.md](../../../CONTEXT.md) (glossary), [Phase 2 doc](../../phases/phase-2-multi-llm-agentic.md)

This is the condensed record of the decisions made while brainstorming and
grilling Phase 2, before the PRD issue was written. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established by
[the Phase 1 design doc](2026-08-23-phase-1-open-network-design.md). The
phase doc is the *what*; this is the *why*.

## What Phase 2 asks for

`docs/phases/phase-2-multi-llm-agentic.md` scoped the goal — host different
models on different nodes, and build an agent whose flow calls across
several of them, making this the first phase where one logical task touches
more than one host — and left five questions explicitly open: where context
lives, what crosses a host boundary, the privacy implications of it
crossing, routing logic, and whether multi-hop latency is even usable.

## The central question, and the fact that answers most of it

The phase doc's own framing, unresolved since the project began:

> "the context either remains at user's machine or is passed to the llm host
> — idk how should we move on that — but somehow we have to pass at least
> some parts from one host to another."

**One of those two options does not exist.** [ADR-0002](../../adr/0002-node-transport-model.md)
recorded live reachability tests proving nodes have outbound access only:
`a6000`/`h100` are private RFC1918 addresses reachable only over the
university VPN, and the HPC login node silently drops every port except one
allow-listed SSH port. A node cannot open a connection to another node.
Host-to-host handoff is therefore architecturally impossible without new
relaying machinery, and the real question is narrower: **client or
coordinator?**

## Decisions made

**Orchestration is client-side; the coordinator stays stateless.** A flow is
N ordinary `complete` round-trips, each naming a model, with the client
holding context between them. Recorded as [ADR-0003](../../adr/0003-client-side-orchestration.md)
— it is hard to reverse, genuinely surprising without the ADR-0002 context,
and the result of a real trade-off. Coordinator-side orchestration was the
serious alternative and was rejected *for now* rather than forever: its one
real advantage is fewer wide-area round-trips, which only matters if
client↔coordinator latency is significant next to inference time. That is an
assumption, so Phase 2 measures it instead of designing around it. Because
the wire protocol is identical either way, adding a coordinator-side
orchestrator later would be additive, not a rewrite.

**A large part of what the phase doc describes already works.** Verified
against the code, not assumed: each node registers its own `--model`, and
`find_node_for_model` filters by exact model string, so "different nodes may
host different models" is *already true today* — start one node with
`--model X` and another with `--model Y` and routing is already correct. This
materially shrank the phase. What is genuinely missing is a client that can
do more than one shot, a way for a client to discover models, context
crossing the wire with its structure intact, and a demonstration that the
whole thing composes.

**Multi-model-per-node is deferred.** The phase doc floats "one node may host
more than one model if its hardware allows." It is a GPU-memory optimization,
not a capability unlock — every Phase 2 goal is reachable by running more
nodes — and it would add per-model process lifecycle, memory arbitration and
per-model health to the node agent. Deferred on the same grounds Phase 1
deferred decentralized discovery and output verification: not load-bearing
for this phase's actual goal.

**Context crosses as a `messages` array, end to end.** `messages: [{role,
content}, ...]` flows client → coordinator → node → vLLM's chat endpoint. The
node already builds exactly this shape — it wraps a single prompt string as
`messages: [{role: "user", content: prompt}]` — so carrying real roles makes
the node *simpler*, not harder. Flattening a multi-hop flow into one user
message would discard the system/user/assistant structure that chat-tuned
models like Qwen2.5-Instruct are trained on, degrading output for no benefit.
The existing `prompt` string stays as a one-message shorthand, so nothing
that works today breaks.

**Sending both `prompt` and `messages` is rejected as ambiguous**, rather
than resolved by a silent precedence rule. A caller who sets both is
confused, and should be told so; silently dropping one is the kind of bug
that is painful to trace back from a wrong answer.

**Model discovery is a new, deliberately minimal client-facing
`list_models`**, returning the distinct model strings currently served and a
healthy-node count for each. It excludes node fingerprints, bound GitHub
identities and reputation counters — those are operator-facing, and a client
token should not enumerate volunteer identities. This leaks nothing material:
a client already holds the shared operator token and can probe any model
string directly, so `list_models` reveals strictly less than the operator
status view and nothing that could not be inferred by trying.

**A model going missing mid-flow fails that hop and surfaces to the agent**,
exactly as today (`no healthy node for model 'X'`). A `list_models` pre-check
is advisory only and can never be a guarantee — a node can disconnect between
the check and the call — so it must not be treated as one. Coordinator-side
substitution of a different model was rejected: in a flow where the agent
chose each model deliberately, silently serving a different one is precisely
the wrong surprise.

**A real defect found while grilling, and folded into the phase.** An
over-length request makes vLLM return HTTP 400; `urllib` raises;
`request_handler`'s broad `except Exception` turns it into a
`complete_error`; the coordinator maps that to `NodeError` and calls
`registry.record_crash(...)`. So *a client sending too much context
permanently damages an innocent volunteer's reputation.* The bug exists
today, but Phase 2 makes it dramatically easier to hit, because context grows
every hop by design. Phase 2 therefore fixes it: distinguish client-caused
faults (4xx — malformed request, context too long) from node-caused ones
(5xx, crash, timeout), and only let genuine node faults touch reputation.

**Staying under the context window is the client's job, and overflow returns
a clear typed error.** Only the agent knows which parts of its context are
droppable, so trimming belongs there. The node reports overflow as a specific,
readable reason rather than a raw `HTTP Error 400: Bad Request`. Silent
truncation on the node was rejected outright: quietly discarding context the
agent believed it had sent produces confidently wrong answers with no signal
at all — the worst available failure mode.

**Privacy is handled by disclosure, with sharper framing — not by a new
mechanism.** Phase 1 disclosed that a volunteer node necessarily sees the
plaintext prompt it serves. Phase 2 widens that materially: several nodes each
see a growing slice of one conversation, including other models' outputs. No
mechanism is added, consistent with Phase 1's finding that hiding a prompt
from the machine computing over it is not feasible. The honest mitigation is
structural rather than cryptographic: because orchestration is client-side,
the agent author decides exactly what each hop carries — and the
documentation should say so plainly rather than implying protection that does
not exist.

**Deliberately unchanged:** client authentication (still the single shared
operator token — opening client-side access remains out of scope, as Phase 1
decided), node identity/cap/ban, reputation-weighted routing, and failure
semantics beyond the reputation fix above. Per-model reputation is
unnecessary: reputation is keyed by node public key and a node serves exactly
one model, so it is *already* per-(node, model).

**The reference agent lives in `examples/`, not in the installed package.**
Mycelium ships primitives plus one demonstration — not an agent framework;
roles, planning and tool-calling abstractions are LangGraph's job, not an
inference router's. Keeping the agent out of `src/mycelium/` makes that
boundary legible and avoids creating a de-facto supported API we would then
owe compatibility on.

## Work breakdown

Seven tracer-bullet slices. Critical path 1 → 3 → 4 → 6; slices 1, 2, 5 and 7
can start in parallel.

1. **`messages` array end-to-end** — a multi-turn array flows client →
   coordinator → node → vLLM and returns a contextually-correct answer;
   `prompt` still works; sending both is rejected.
2. **`list_models` discovery** — a client can list currently-served models
   without seeing node identities or reputation.
3. **Multi-hop client library** (blocked by 1) — a client-side API that holds
   context across calls. The primitive agents are written against.
4. **Reference agent** (blocked by 3) — one concrete two-model flow in
   `examples/`, demonstrating the primitives compose.
5. **Privacy disclosure update** — docs only; the multi-hop exposure, framed
   honestly.
6. **Live verification + latency** (blocked by 4) — real hardware, two
   genuinely different models, real flow; measures per-hop and end-to-end
   latency, answering the phase doc's own open question with data. Matches
   the live-verification precedent of issues #4/#6/#25/#49.
7. **Client-vs-node error classification** — the reputation defect above.

Slices 3 and 4 are kept separate deliberately, so the library is designed as
a library rather than as whatever the one demo happened to need. Each slice
documents its own feature in `docs/OPERATIONS.md`; slice 5 stays a focused
disclosure rewrite rather than becoming a catch-all documentation ticket.

## Explicitly out of scope for Phase 2

Streaming responses. Conversation persistence (a flow lives in one client
process). Capability-based routing ("any coding model") — model strings stay
exact. Multi-model-per-node. Opening client-side access to the public
(inherited from Phase 1). Payments, training, multi-tenant SLAs (global
non-goals). Anything Phase 3 — layer/pipeline splitting of a single model
across farms remains a separate, later phase.
