# Phase 2 — Multi-LLM Agentic Flow — Design

Date: 2026-09-04
Status: Approved, PRD published as issue #54, not yet implemented
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

**Privacy: a real mechanism for the problem Phase 2 actually introduces —
aggregation — plus continued disclosure for the part that stays unfixable.**
This decision was revisited after an initial draft proposed disclosure alone.

The distinction that makes a mechanism possible: Phase 1's problem was
*disclosure* — a node reads the plaintext it serves — and that remains
genuinely unfixable. Phase 2's *new* problem is *aggregation*: as a flow
progresses, each successive node can see the original task and every prior
model's output, so per-volunteer exposure grows with flow length even though
no single hop reveals more than Phase 1 already did. Aggregation is tractable
precisely because orchestration is client-side.

Hardware mitigations were investigated and rejected as **unavailable, not
undesirable**: GPU confidential computing (TEE) exists only on Hopper H100 and
newer, and the Ampere A6000s this project actually runs on cannot do it at
all; it further requires SEV-SNP/TDX CPUs, a specific virtualization stack,
and an attestation service. A mechanism excluding every non-Hopper volunteer
does not fit a network built on donated hardware. FHE is orders of magnitude
too slow for inference.

The mechanism adopted is **explicit per-hop context** ([ADR-0004](../../adr/0004-explicit-per-hop-context.md)):
the client library records the whole flow locally but sends only what each
call names. There is no implicit accumulator — the thing every conventional
agent library does, and the thing that would make every node see everything.
This makes the property *structural rather than a setting*: the library has no
code path that sends unnamed content, so it cannot be left switched off. A
"full context by default, opt in to restrict" design was rejected for exactly
that reason — defaults decide real behavior, and almost nobody would restrict.
The `Flow` additionally reports what each node and identity actually received,
so the property can be checked rather than trusted.

What this does **not** do: it does not make inference private. The node
serving a hop still reads that hop. It narrows *how much* each volunteer
sees, not *whether* they see it — and the documentation must keep saying so
plainly rather than implying protection that does not exist.

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
3. **Multi-hop client library with explicit per-hop context** (blocked by 1) —
   a `Flow` that records the whole flow locally but sends only what each call
   names ([ADR-0004](../../adr/0004-explicit-per-hop-context.md)), plus a
   per-node/per-identity exposure report so the property is checkable. The
   primitive agents are written against.
4. **Reference agent** (blocked by 3) — one concrete two-model flow in
   `examples/`, demonstrating the primitives compose and that a real agent is
   workable under explicit-context rules.
5. **Privacy documentation** — docs only; documents both the new mechanism and
   the disclosure that survives it, without overclaiming.
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

## Live verification (2026-09-06 to 2026-09-16, issue #61)

Run per the plan in [issue #61's design doc](2026-09-06-issue-61-live-verification-design.md):
client on the operator's Mac, coordinator and two nodes on `a6000`, two
genuinely different real models, ten measured flows after one discarded
warm-up. Spread across ten real-world days — not because the runbook was
wrong, but because of two independent operational failures below, both
resolved without changing the design.

### Attempt 1 — halted, recorded separately

A first attempt on 2026-09-06 got through setup and then discovered the box
could not run CUDA at all: GPU 3 had fallen off the bus and taken the
driver's CUDA state down with it, so `torch.cuda.device_count()` returned 0
on every GPU, not just the broken one. Full diagnosis in
[issue #61's design doc](2026-09-06-issue-61-live-verification-design.md#attempt-1--halted-before-measurement-2026-09-06).
Fixed by a reboot outside this session's control. Confirmed fixed on
2026-09-13 with a real matmul on GPU 3, not just `nvidia-smi` listing it.

### Setup, attempt 2

`a6000`'s checkout was five PRs behind (`e30b77f`, predating all of
Phase 2); updated to `main` (`468a497` — before #56/#60 merged, since #60's
own live-hardware dependency chain meant this session had to run before
either could land). `Qwen/Qwen2.5-1.5B-Instruct` fetched, `Flow` and the
new console scripts confirmed importable.

**GPU reassignment.** By the time the reboot cleared and the session
actually ran, another user (`somya`) had a real training job
(`dirichlet_train.py`) on GPU 0 — the GPU the runbook had assigned to the
1.5B node. Moved that node to GPU 2 instead, per the standing rule to adapt
around real jobs rather than compete. GPU 0 was never touched.

**Two operational failures, neither a code defect:**

1. **The VPN path to `a6000` dropped repeatedly** — three separate times
   across the session, each recovering on its own after some delay. Not
   investigated further; it is infrastructure outside this project's
   control, and the session simply waited each one out and re-verified
   state before continuing.
2. **The cached GitHub token from issue #49's session had expired**, and its
   replacement device-flow authorization raced the VPN drops and the
   device code's own short lifetime more than once — including one
   `SSL: CERTIFICATE_VERIFY_FAILED` that resolved on retry and did not
   recur once genuinely stable connectivity held. The box's CA bundle was
   checked directly and confirmed intact; a live `urlopen` reached
   `github.com` cleanly on the same attempt. This is recorded as
   transient network flakiness coincident with the VPN issue above, not a
   trust-store defect.

**Discoverability criterion, partially met.** #61 asks that both models be
"discoverable by a client." Client-side discovery is #56
(`list_models`/`Flow.list_models()`), which had not merged when this
session had to run — #60's own live-hardware dependency put this session
ahead of it in the queue. This was anticipated and accepted in
[issue #61's design doc](2026-09-06-issue-61-live-verification-design.md#a-criterion-that-cannot-be-fully-met)
before the session began, not discovered here. What was verified instead is
that both models are genuinely served by two real nodes, confirmed through
`mycelium-coordinator-status` below — the operator's view, not a client's.
No client in this session ever called `list_models`; that check is #56's own
to run once it lands.

Both nodes registered cleanly once connectivity held for long enough:

```
node-a-15b [e707f8b596ce] (github:Varun-Gambhir) [ok:0 ...]: Qwen/Qwen2.5-1.5B-Instruct
node-b-7b  [15aa86a53a55] (github:Varun-Gambhir) [ok:0 ...]: Qwen/Qwen2.5-7B-Instruct
```

### Scenario 1 — the reference agent, once, unmodified

```
$ python examples/draft_then_critique.py \
    --coordinator-url wss://192.168.22.23:18765 \
    --coordinator-cert cert.pem --token-file token \
    --draft-model Qwen/Qwen2.5-1.5B-Instruct \
    --critique-model Qwen/Qwen2.5-7B-Instruct

hop 0 -> Qwen/Qwen2.5-1.5B-Instruct
  sent user: Explain why the sky is blue, in two sentences.
  got: The sky appears blue because the Earth's atmosphere scatters sunlight
  in all directions, with shorter wavelengths (blue) being scattered more
  than longer wavelengths (red)... This effect is known as Rayleigh
  scattering.

hop 1 -> Qwen/Qwen2.5-7B-Instruct
  sent user: Improve this answer to: Explain why the sky is blue...
  sent assistant: The sky appears blue because the Earth's atmosphere...
  got: The sky appears blue because the atmosphere scatters shorter
  wavelength blue light more than longer wavelength red light... This
  phenomenon, called Rayleigh scattering, makes the sky predominantly blue
  during clear daytime conditions.

who saw what:
  node 2fd3cfe8496ce176 saw hops [0]
  node 08551b5ab72cc92e saw hops [1]
  identity 67e3d8aa66bbe64d saw hops [0, 1]
```

The critique hop demonstrably improved the draft (tighter phrasing, the same
factual content), using exactly the two messages it named — visible directly
in the printed output, satisfying that acceptance criterion by inspection
rather than trust. The exposure report is correct on its face: two distinct
volunteer machines, one bound identity — accurate, since both nodes in this
session are registered under the same GitHub account.

### Scenario 2 — ten measured flows, one warm-up discarded

Driven by a small unshipped harness on the operator's Mac (per the design's
decision that latency measurement is not the reference agent's job — see
[issue #61's design doc](2026-09-06-issue-61-live-verification-design.md#latency-comes-from-a-throwaway-harness-not-from-the-reference-agent)).
Source:

```python
async def one_flow():
    flow = Flow(URL, CERT, TOKEN)
    draft = await flow.call(DRAFT_MODEL, messages=[user(TASK)])
    await flow.call(
        CRITIQUE_MODEL,
        messages=[user(f"Improve this answer to: {TASK}"), assistant(draft.text)],
    )
    return flow
```

```
warm-up flow (discarded) ...
  warm-up total wall 3044ms
run  1  total_wall=2417ms  hop0 wall=864 elapsed=321 overhead=543  hop1 wall=1553 elapsed=1156 overhead=397
run  2  total_wall=2219ms  hop0 wall=738 elapsed=437 overhead=301  hop1 wall=1481 elapsed=1204 overhead=277
run  3  total_wall=2043ms  hop0 wall=667 elapsed=364 overhead=303  hop1 wall=1376 elapsed=1068 overhead=308
run  4  total_wall=1964ms  hop0 wall=536 elapsed=235 overhead=301  hop1 wall=1428 elapsed=1113 overhead=315
run  5  total_wall=2046ms  hop0 wall=676 elapsed=368 overhead=308  hop1 wall=1370 elapsed=1068 overhead=302
run  6  total_wall=1899ms  hop0 wall=668 elapsed=305 overhead=363  hop1 wall=1231 elapsed=910 overhead=321
run  7  total_wall=1859ms  hop0 wall=636 elapsed=324 overhead=312  hop1 wall=1223 elapsed=887 overhead=336
run  8  total_wall=2041ms  hop0 wall=643 elapsed=325 overhead=318  hop1 wall=1398 elapsed=1111 overhead=287
run  9  total_wall=1733ms  hop0 wall=632 elapsed=281 overhead=351  hop1 wall=1101 elapsed=798 overhead=303
run 10  total_wall=1926ms  hop0 wall=683 elapsed=372 overhead=311  hop1 wall=1243 elapsed=934 overhead=309

=== medians over 10 flows ===
hop0 (Qwen/Qwen2.5-1.5B-Instruct)
   wall     median      668ms   range 536-864ms
   elapsed  median      324ms   range 235-437ms
   overhead median      312ms   range 301-543ms
hop1 (Qwen/Qwen2.5-7B-Instruct)
   wall     median     1373ms   range 1101-1553ms
   elapsed  median     1068ms   range 798-1204ms
   overhead median      308ms   range 277-397ms

end-to-end wall   median 2002ms   range 1733-2417ms
total overhead    median 618ms   range 578-940ms   (30.9% of wall)
```

`elapsed_ms` is the coordinator's own measurement, spanning from just before
its routing loop to the reply — node lookup, the node↔coordinator leg
(loopback here, both nodes on the same box as the coordinator), and
inference. `overhead` (`wall_ms − elapsed_ms`) is what remains: the client's
TLS handshake for a fresh per-hop connection (deliberately not reused — see
[the Phase 2 implementation design](2026-09-04-phase-2-implementation-design.md#a-flow-costs-n1-extra-clientcoordinator-round-trips)),
plus the network transit time for the request and reply to physically cross
the real ~41ms-RTT path between the Mac and `a6000`.

### Scenario 3 — exposure cross-check

The client's exposure report was checked against the coordinator's own
independent completion counters, not trusted on its own:

```
$ mycelium-coordinator-status ...
node-a-15b [e707f8b596ce] (github:Varun-Gambhir) [ok:12 timeout:0 crash:0 disconnect:0 client_faults:0]: Qwen/Qwen2.5-1.5B-Instruct
node-b-7b  [15aa86a53a55] (github:Varun-Gambhir) [ok:12 timeout:0 crash:0 disconnect:0 client_faults:0]: Qwen/Qwen2.5-7B-Instruct
```

12 completions on each node exactly matches what ran: 1 reference-agent flow
+ 1 warm-up + 10 measured = 12 flows, each one hop-0 completion on
`node-a-15b` and one hop-1 completion on `node-b-7b`. Zero timeouts, crashes,
disconnects or client faults. The client-reported node handle for each hop
was stable across all ten measured flows (`{'2fd3cfe8496ce176'}` for hop 0,
`{'08551b5ab72cc92e'}` for hop 1, both singleton sets) — the report is
accurate against what the nodes actually received, which is the property
this scenario exists to check.

### Conclusion: is multi-hop latency usable?

**Yes**, resting on the numbers above, with one honest qualification.

Client↔coordinator overhead is real and not negligible **as a fraction** —
roughly 300ms per hop, ~31% of end-to-end wall time in this session. That is
higher than
[ADR-0003](../../adr/0003-client-side-orchestration.md)'s framing of
"expected to be noise against multi-second inference" would suggest, and the
reason is specific to this session's choice of models: the 1.5B and 7B
instruct models used here run inference in the hundreds-of-milliseconds to
low-single-second range, not the multi-second range ADR-0003's assumption
was reasoning about. Against a genuinely multi-second-per-hop model, the
same ~300ms of fixed per-hop overhead would be a much smaller fraction —
this session used fast models specifically because #61's own design
prioritized "fast to load, unambiguously different" over "representative of
a slow production model."

In **absolute** terms, the overhead stays small and end-to-end latency for
this two-hop flow lands at ~2 seconds median — well within interactive
bounds for the kind of agentic flow Phase 2 targets. Nothing here contradicts
ADR-0003's decision or its own reversibility clause: the wire protocol is
identical either way, so if a future, slower-inference workload showed
overhead becoming a materially larger fraction of a longer total, a
coordinator-side orchestrator remains additive rather than a rewrite. That
remains untested by this session and is exactly the kind of finding this
project records rather than assumes.

### Environment findings carried forward

- GPU 3's earlier fault (recorded in Attempt 1) is resolved; confirmed with
  a real CUDA matmul, not just `nvidia-smi` enumeration.
- `~/.mycelium/github-token` now holds a token generated during this
  session (2026-09-16), replacing the stale one from issue #49. Left in
  place deliberately, at the default path a future node expects, so the
  next live session does not have to redo this session's device-flow
  fight.
- The VPN path between the operator's Mac and `a6000` dropped three times
  across this session with no visible pattern. Worth knowing for whoever
  plans the next live session on this box — build in slack for it rather
  than assuming a single continuous connection.

### Teardown

Both `mycelium-node` processes and their `vllm serve` children stopped and
confirmed gone (`pgrep` empty); the coordinator stopped and port 18765
confirmed closed; `~/.mycelium61/` removed. GPUs 1 and 2 back to idle; GPU 0
was never touched and `somya`'s job continued undisturbed throughout. The
checkout was left on `main`; the downloaded 1.5B weights were left cached.
