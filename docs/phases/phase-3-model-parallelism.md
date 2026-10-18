# Phase 3 — Model Parallelism (Layer Splitting)

Status: Designed, not yet built
Depends on: [Phase 2](phase-2-multi-llm-agentic.md) multi-host mechanics
Related: [Phase 3 design rationale](../superpowers/specs/2026-10-18-phase-3-model-parallelism-design.md),
[ADR-0005 — coordinator-chained pipeline relay](../adr/0005-coordinator-chained-pipeline-relay.md),
[CONTEXT.md](../../CONTEXT.md) (glossary)

## Goal

Let several single-GPU volunteers jointly serve a 70B-class model, at full
bf16 precision, that none of them can hold alone: split its layers across
stage nodes, passing activations from stage to stage mid-inference, at a
speed usable for interactive chat.

## What's decided

- **Target: `Qwen/Qwen2.5-72B-Instruct` in bf16, four stages of 20 layers
  on 48 GB A6000s.** Quantization is ruled out — a 4-bit 70B fits on one
  A6000 and would make this phase unnecessary. Multi-GPU boxes already
  serve such models via vLLM's in-node tensor parallelism; Phase 3 is for
  volunteers who each have one GPU.
- **Speculative decoding is in scope.** Single-stream pipeline decode reads
  every weight per token, sequentially across stages — ~5.5 tok/s at best
  on A6000s before any network cost (estimate). A draft model
  (`Qwen/Qwen2.5-0.5B-Instruct`) proposing several tokens per pass is the
  only way to an interactive target. Target: **≥ 5 tok/s single-stream
  decode, measured on live hardware**, not guaranteed.
- **The coordinator chains the stages**
  ([ADR-0005](../adr/0005-coordinator-chained-pipeline-relay.md)). Nodes are
  outbound-only (ADR-0002), so activations relay S1 → coordinator → S2 →
  … → client. The coordinator holds per-session routing (which nodes are
  pinned to a generation), never content.
- **Stage nodes run our own transformers-based stage server**, not vLLM.
  vLLM's pipeline parallelism needs a trusted direct-connectivity cluster;
  Petals is dormant (last release September 2023). A node serves either a
  model or a stage, never both.
- **A stage node declares its layer range** (`--layers 20-39`), matched
  exactly; the coordinator assembles a contiguous covering chain per
  session or fails immediately naming the missing ranges.
- **The client holds the embedding layer, LM head and draft model**, so
  nodes see hidden states only — never token IDs or logits.
- **A failed stage surfaces as a node fault; the client library resumes by
  replay** onto a fresh chain, since it holds every token. The coordinator
  never retries silently.
- **Privacy: wider exposure than Phase 1, disclosed plainly.** Hidden states
  can be largely inverted to recover input, and every stage sees every token
  of a session — four volunteers per conversation. ADR-0004's explicit
  context cannot help inside a pipeline. Nothing here makes inference
  private.
- **Identity, cap, ban and reputation apply to stage nodes unchanged.**

## Non-goals

Batching sessions for aggregate throughput. Quantization. Dynamic layer
assignment. Direct node-to-node transport. Streaming tokens beyond
per-pass returns. Multi-model-per-node. Opening client access to the
public (inherited).

Inherited global non-goals: payments/incentive mechanisms, model
training/fine-tuning, multi-tenant SLAs — see Readme §3.
