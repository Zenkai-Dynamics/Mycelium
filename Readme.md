**PRODUCT REQUIREMENTS DOCUMENT**

# Mycelium

A framework for running LLM inference across GPUs volunteered by geographically separated users.

Working product definition · v0.2 · 12 August 2026 · Status: Draft, Phase 0 about to start build

> **Name**
> Mycelium — the underground fungal network that routes resources between trees across a forest with no central trunk. Fits the shape of the product: independently owned nodes, connected through a coordinator, growing from a small trusted pool into a public network over time. See [ADR-0001](docs/adr/0001-project-name.md).

> **Relationship to Bailment**
> Mycelium started as a brainstorm inside the sibling `Bailment` repo before moving to its own repo. Bailment's thesis ("ship tasks, not tokens") is explicitly the opposite of what Mycelium does (move model weights/activations/tokens between hosts) — the two should not be merged or made to share architecture.

---

## 1. Problem Statement

Running inference on a capable LLM requires GPU hardware that most individuals and small labs don't own outright, while many people *do* have occasional access to idle or underused GPU capacity — a lab workstation, a campus HPC allocation, a home rig — that sits unused most of the time. There's no lightweight way to pool that scattered, occasional capacity into something that behaves like a single inference service, the way BitTorrent pools scattered bandwidth/storage or BOINC pools scattered compute cycles.

## 2. Solution

Mycelium lets a user opt a GPU machine in as a **node**: it registers with a coordinator, hosts an LLM, and serves inference requests routed to it. A client talks to one stable endpoint (the coordinator) without needing to know which physical machine actually runs the model. The system is built in phases, each one a strict superset of the last, so the hard problems (public trust, multi-model orchestration, model parallelism) are deferred until the basic mechanics are proven — and each phase gets its own doc, kept up to date as that phase is actually designed and built.

## 3. Phases

Each phase links to its own document — the full architecture and open questions for that phase live there, not here, so this PRD stays an overview.

| Phase | Scope | Status | Doc |
|---|---|---|---|
| **0** | One LLM. A small, closed pool of nodes the operator personally controls (HPC allocations, VPN-gated lab machines). Prove client → coordinator → node → response round-trips end to end. | **Building now** | [phase-0-foundation.md](docs/phases/phase-0-foundation.md) |
| **1** | Open the pool to public, opt-in volunteer nodes — torrent/BOINC-style. Still one LLM per request; trust and abuse-prevention become real concerns. | Designed, not yet built ([PRD: issue #31](https://github.com/Zenkai-Dynamics/Mycelium/issues/31)) | [phase-1-open-network.md](docs/phases/phase-1-open-network.md) |
| **2** | Multiple different LLMs hosted across different nodes, composed by an agent's multi-LLM flow. Context/prompt state gets passed between hosts as the agent moves between models. | Future, not yet designed | [phase-2-multi-llm-agentic.md](docs/phases/phase-2-multi-llm-agentic.md) |
| **3** | Split a single LLM's *layers* across multiple GPU farms (pipeline/model parallelism) for models too large for any one farm to hold; activations pass host to host mid-inference. | Far future, not yet designed | [phase-3-model-parallelism.md](docs/phases/phase-3-model-parallelism.md) |

Phase 0 and Phase 1 have now been designed in depth; Phases 2–3 docs still only record what was decided about *scope* during the initial brainstorm, plus the open questions that need their own brainstorming pass when each phase's turn comes — they are deliberately not filled in with invented detail.

Non-goals for *every* phase covered by this document: payments/incentive mechanisms, model training or fine-tuning, multi-tenant SLAs. These may become relevant far downstream but are not shaping any decision made here.

---

## 4. Prior Art

Worth being aware of, not treated as a dependency: **Petals** (BigScience) has already built public, volunteer-run layer-split inference for large models — directly relevant to Phase 3. **Hivemind**, also BigScience, is a DHT-based library for decentralized coordination among untrusted peers — considered for Phase 1's discovery mechanism and explicitly deferred (Phase 1 keeps a centralized coordinator; decentralized discovery is now pushed to Phase 2/3 territory or a later revisit — see [phase-1-open-network.md](docs/phases/phase-1-open-network.md)). Neither is assumed as a dependency here; noted so later design doesn't reinvent solved problems unknowingly.

## 5. Documentation Map

- `docs/SETUP.md` — developer setup guide: clean checkout to a working dev environment, covering the platform-agnostic base install and the Linux/GPU node extra.
- `docs/OPERATIONS.md` — operations guide: running the system for real — generating a token, starting a coordinator, connecting a node, sending a client request.
- `docs/phases/` — one doc per phase (linked above), the living source of truth for that phase's scope, architecture, and open questions. Update these in place as each phase is actually built, rather than treating this Readme as the record.
- `docs/adr/` — architecture decision records (e.g. the project name).
- `docs/superpowers/specs/` — dated design-session records, capturing *why* a decision was made at the time, left as-written rather than updated.
- `docs/dependencies.md` — pinned dependency versions and the CUDA/driver/GPU hardware compatibility they assume. Update in place as pins change.

---

**Note to reviewer:** This PRD intentionally stops at overview level per the operator's request — it is not an implementation plan, exhaustive user-story catalog, or task breakdown. Phase-level detail (architecture, success criteria, user stories, open risks) lives in each phase's own doc under `docs/phases/`, so it can be extended without growing this file into a monolith. An implementation plan comes later, scoped to Phase 0 only, once its doc's open risks are resolved.
