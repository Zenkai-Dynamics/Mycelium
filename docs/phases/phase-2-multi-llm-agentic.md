# Phase 2 — Multi-LLM Agentic Flow

Status: Future — not started, not brainstormed in depth yet
Depends on: [Phase 1](phase-1-open-network.md) network of nodes

## Goal

Host multiple *different* LLMs across different nodes (one node may host
one model, or more than one if its hardware allows), and build an agent
whose agentic flow calls across several of these models rather than just
one. This is the first phase where a single logical task touches more
than one host.

## What's decided

- Different nodes may host different models — no longer "one model,
  many interchangeable nodes" as in Phases 0–1.
- The reason for multiple models is an agent that uses them together in
  one agentic flow (e.g. different models for different steps/roles).
- At least *some part* of the working context has to move from one host
  to another for a multi-hop agentic flow to work at all — this much is
  a hard constraint, not a choice.

## Open questions — the central one is explicitly unresolved

This was flagged as an open question at the very start of brainstorming
and has not been answered since:

> "the context either remains at user's machine or is passed to the llm
> host — idk how should we move on that — but somehow we have to pass at
> least some parts from one host to another."

Specifically undecided:

- **Where does context live by default?** Does the client hold the full
  conversation/task state and re-send whatever's needed on each hop
  (stateless per-hop, like Phases 0–1), or does state get handed off
  host-to-host directly?
- **What crosses a host boundary vs. stays local?** If only "some parts"
  move, which parts, and on what basis?
- **Privacy/trust implications of state crossing a boundary.** Once
  context (not just a single prompt) reaches a second, third host, more
  of a conversation is exposed to more parties than in Phase 0/1's
  single-hop model. Not analyzed.
- **Routing logic.** How does the agent decide which model/node handles
  each step of its flow?
- **Latency/geography.** Multiple hops across geographically separated
  hosts within a single agent turn — no sense yet of whether this is
  fast enough to be usable.

## Non-goals (inherited)

Payments/incentive mechanisms, model training/fine-tuning, multi-tenant
SLAs — see Readme §3.
