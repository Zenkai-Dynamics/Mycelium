# Phase 3 — Model Parallelism (Layer Splitting) — Design

Date: 2026-10-18
Status: Approved, PRD published as issue #77, not yet implemented
Related: [ADR-0005 — coordinator-chained pipeline relay](../../adr/0005-coordinator-chained-pipeline-relay.md),
[ADR-0002 — node transport model](../../adr/0002-node-transport-model.md),
[ADR-0003 — client-side orchestration](../../adr/0003-client-side-orchestration.md),
[CONTEXT.md](../../../CONTEXT.md) (glossary), [Phase 3 doc](../../phases/phase-3-model-parallelism.md)

This is the condensed record of the decisions made while brainstorming and
grilling Phase 3, before the PRD issue was written — the same pattern as
[the Phase 2 design doc](2026-09-04-phase-2-multi-llm-agentic-design.md).
The phase doc is the *what*; this is the *why*.

## What Phase 3 asks for

`docs/phases/phase-3-model-parallelism.md` scoped the goal — split one
model's layers across several hosts so hosts that can each hold only part
of it still contribute — and left five questions open: partitioning
strategy, latency budget, mid-pipeline failure handling, build vs. adopt
Petals, and activation privacy.

## First: does Phase 3 have a real target?

The phase doc says "a model too large for any one GPU farm". That had to be
made concrete before anything else, because two cheaper answers exist:

- **vLLM already tensor-parallelizes across the GPUs inside one node.** A
  multi-GPU box holds a 70B model in bf16 today with no Phase 3 at all.
- **A 4-bit 70B (~40 GB) fits on one 48 GB A6000.** vLLM serves AWQ/GPTQ
  natively, so for this project's hardware quantization makes layer
  splitting unnecessary.

Settled with the operator: the target is **pooling single-GPU volunteers
to serve a 70B-class model none of them can hold alone, at full bf16
precision** — quantization is ruled out. That is the population Phase 3
serves, and the only one for which neither cheaper answer works.

## The latency fact that reshaped the design

The operator's success bar is **interactive chat speed**. Grilling that
against arithmetic found a floor that no topology removes:

Single-stream decode is memory-bandwidth bound — every token reads every
weight. A bf16 70B-class model is ~140–145 GB. In a pipeline, stages run
*sequentially* for one request, so splitting does not parallelize that read
(tensor parallelism inside one box does; that is exactly what Phase 3
cannot use across volunteers). At an A6000's ~768 GB/s, that is ~180 ms per
token — **~5.5 tok/s at best with zero network cost**, and ~3–4 tok/s once
~100 ms of relayed hops per token are added. These are estimates, not
measurements; the live-verification ticket replaces them with data.

The one lever that moves this is **speculative decoding**: a small draft
model proposes k tokens and one pipeline pass verifies all k, amortizing
both the weight read and the network hops across several tokens. Petals had
a draft PR for exactly this before going dormant. **Speculative decoding is
therefore in Phase 3's scope** — without it the interactive goal is
unreachable by construction. The target is **≥ 5 tok/s single-stream
decode, measured**, not guaranteed — the same "measure, don't assume"
stance Phase 2 took on latency.

## Decisions made

**Target model: `Qwen/Qwen2.5-72B-Instruct` (bf16), draft
`Qwen/Qwen2.5-0.5B-Instruct`.** Same family as the 7B validated in Phase 0,
not gated on Hugging Face (Llama-3.1-70B would add a per-volunteer access
sign-off), and the draft shares the target's tokenizer, which speculative
decoding requires. 80 decoder layers at ~145 GB means four A6000 stages of
20 layers each (~36 GB of weights plus KV cache). The two models' vocab
*sizes* differ in padding (151936 vs 152064) despite the shared tokenizer —
verifying that draft and target logits compare correctly over the shared
vocab is an implementation detail for the client ticket, flagged here so it
isn't discovered late.

**Topology: the coordinator chains the stages.** See
[ADR-0005](../../adr/0005-coordinator-chained-pipeline-relay.md). Nodes are
outbound-only (ADR-0002), so activations can cross only via the
coordinator or the client. Three options were weighed:

- *Coordinator chains* (chosen) — the client sends one verify pass; the
  coordinator forwards activations S1 → S2 → S3 → S4 over the nodes'
  existing connections and returns the result. One relay leg per stage
  boundary; ADR-0002 untouched.
- *Client drives each stage* (Petals' model) — keeps the coordinator
  stateless, consistent with ADR-0003, but every stage costs a full
  client↔coordinator round trip, roughly doubling WAN legs. Worst fit for an
  interactive target.
- *Direct node-to-node via NAT traversal / an overlay* — lowest latency,
  but reopens ADR-0002, hole punching through institutional firewalls often
  fails (so a relay fallback is needed anyway), and gives volunteers direct
  reachability to one another, a trust-model change. Rejected.

Speculative decoding amortizes the coordinator relay's extra hop, which is
what makes the simplest topology acceptable.

**The coordinator gains its first per-session state — routing only.** A
stage holds a KV cache, so a generation must hit the same nodes on every
pass. The coordinator keeps `session_id → [pinned nodes]` for a
generation's lifetime, dropped on session end, node fault, or idle
timeout. It never holds token history, prompts or activations beyond the
pass in flight. ADR-0003's statelessness is *scoped*, not reversed:
coordinator state is still never conversation content.

**Stage engine: our own stage server on Hugging Face transformers.** It
loads only the declared layer range, keeps one KV cache per session, and
runs `forward(hidden_states) → hidden_states`. Alternatives rejected:

- *vLLM's own pipeline parallelism* assumes a trusted Ray/NCCL cluster with
  direct connectivity — unusable for untrusted, outbound-only volunteers.
- *Hooking vLLM internals* — best kernels and paged KV cache, but couples
  to vLLM 0.25's private worker APIs, which break on upgrade, on a version
  already pinned by `ray[llm]`.
- *Vendoring Petals' server* — Petals is effectively dormant: last release
  v2.2.0 (September 2023), last push to the repository September 2024
  (checked 2026-10-18, not archived). It pins far older
  transformers/torch than this repo's torch 2.11 stack; porting it would
  cost more than writing the narrow piece needed.

Single-stream decode is bandwidth-bound regardless, so vLLM's batching
advantage is small for this goal. A node runs either a model (vLLM, as in
Phases 0–2) or a stage, never both.

**Partitioning: a node declares its range, matched exactly.** A stage node
registers `--model Qwen/Qwen2.5-72B-Instruct --layers 20-39`. At session
start the coordinator assembles a chain of healthy nodes covering the
model's layers contiguously, preferring reputation as routing does today,
or fails immediately naming the missing ranges. This mirrors exact model
string matching. Rejected: a fixed per-model shard table (rigid against
heterogeneous VRAM) and coordinator-assigned dynamic ranges (reloading
weights takes minutes, and it adds a large control plane).

**Embedding layer, LM head and draft model live on the client.** Petals'
approach: nodes only ever see hidden states, never token IDs or logits. The
client needs roughly 5 GB for Qwen2.5-72B's embedding and untied LM head,
plus the 0.5B draft model — a capable client is required anyway once it
runs the draft. The alternative (first/last stages hold them, thin client)
was simpler but puts plaintext tokens and outputs on volunteers for no
gain.

**Mid-pipeline failure: surface, client resumes by replay.** A stage that
drops, times out or crashes fails the pass with a typed node fault,
reputation is recorded as today, the session's chain is dropped, and the
other stages free that session's KV cache. Because the client holds every
token so far (it drives the draft/verify loop), the client library can open
a fresh session and re-prefill prompt plus accepted tokens, with bounded
attempts. The coordinator never retries silently, consistent with Phases
0–2. Rejected: the coordinator swapping only the failed stage (it would
have to hold token history, breaking "relays, never remembers content"),
and surface-only (any volunteer drop loses a long chat's progress).

## Failure handling, in full

- **No covering chain** — session start fails immediately with a typed
  error naming the missing layer ranges. No queueing.
- **Stage drop / timeout / crash mid-pass** — typed node fault, reputation
  recorded, chain dropped, KV caches freed, client replays (above).
- **Client faults** — malformed hidden-state shape, unknown session,
  context over the model's window — typed client fault with no reputation
  damage, reusing #58's classification.
- **Abandoned sessions** — an idle timeout on both the coordinator and each
  stage frees routing state and KV cache, so a dead client cannot pin
  volunteer VRAM indefinitely.
- **Message size** — prefill activations are ~16 KB per token per boundary
  (hidden size 8192 × bf16), so a 2k-token prompt is ~32 MB. The websocket
  size limit is raised for stage traffic only, and prompt length is capped
  and checked client-side.

## Trust and privacy

Identity, per-identity cap, ban and reputation-weighted selection apply to
stage nodes unchanged; the operator-token decision (#68) is unchanged.

Nodes see only hidden states. **This does not make inference private.**
Hidden states can be largely inverted to recover the input, and every stage
sees every token of the session — so a four-stage pipeline puts the *whole*
conversation in front of four volunteers per session, wider exposure than a
Phase 1 single hop. ADR-0004's explicit per-hop context cannot help inside
a pipeline: every stage needs every token. The docs must say so with the
same bluntness as #57.

## Testing

- Unit and integration tests follow the existing style — real local
  websockets, fake externals. The stage engine is tested against a tiny
  random-weight Qwen2 config on CPU: output split across stages must equal
  single-process output, logits within tolerance.
- Chain assembly: gaps, overlaps, reputation preference, pinning.
- Failure paths: a node drop mid-pass triggers replay; idle timeout frees
  state.
- Speculative decoding under greedy sampling must reproduce plain greedy
  decoding exactly.
- **Live verification is its own ticket** (precedent: #25, #49, #61): four
  stage nodes on four A6000 GPUs serving Qwen2.5-72B bf16 and a client on a
  separate machine; output correctness against a reference, tok/s with and
  without speculation against the ≥ 5 tok/s target, and a stage killed
  mid-generation to confirm replay.

## Non-goals

Batching several sessions through one pipeline for aggregate throughput.
Quantization. Dynamic layer assignment. Direct node-to-node transport.
Streaming tokens to the caller beyond what the client library returns per
pass. Multi-model-per-node, and a node serving a model and a stage at once.
Inherited: payments/incentives, training/fine-tuning, multi-tenant SLAs,
opening client access to the public.
