# Phase 3 — Model Parallelism (Layer Splitting)

Status: Far future — not started, not brainstormed in depth yet
Depends on: [Phase 2](phase-2-multi-llm-agentic.md) multi-host mechanics

## Goal

Handle a single LLM too large for any one GPU farm to load in full: split
its layers across multiple farms, so a farm that can only hold part of
the model still contributes. Activations are passed from farm to farm
mid-inference, rather than a full response being generated on one host.

## What's decided

- The split unit is *layers*, not whole models — this is pipeline/model
  parallelism, distinct from Phase 2's "different whole models on
  different hosts."
- Activations (not just prompts or final text) cross host boundaries
  mid-inference — a new category of data-in-transit not present in any
  earlier phase.
- This is explicitly the last phase, deferred furthest, because it's the
  most technically demanding piece — the earlier phases exist partly to
  avoid front-loading this complexity.

## Open questions — nothing below has been designed yet

- **Partitioning strategy.** How are layers assigned to farms — static
  split decided in advance, or dynamic based on what each farm can hold?
- **Latency budget.** Activations crossing a WAN between geographically
  separated farms, potentially many times per inference request — whether
  this is viable for interactive use at all hasn't been analyzed.
- **Mid-pipeline failure handling.** What happens if a farm holding a
  middle slice of the model drops mid-request?
- **Build vs. adopt.** [Petals](https://github.com/bigscience-workshop/petals)
  (BigScience) already does volunteer-run layer-split inference — see
  Readme's Prior Art. Not yet decided whether Phase 3 builds on/adapts it
  or is built independently.
- **Activation privacy.** Same category of concern as Phase 2's context
  question, one level lower: every farm in the pipeline sees the
  activations passing through it, which can leak information about the
  original prompt. Not analyzed.

## Non-goals (inherited)

Payments/incentive mechanisms, model training/fine-tuning, multi-tenant
SLAs — see Readme §3.
