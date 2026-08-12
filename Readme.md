**PRODUCT REQUIREMENTS DOCUMENT**

# Mycelium

A framework for running LLM inference across GPUs volunteered by geographically separated users.

Working product definition · v0.1 · 12 August 2026 · Status: Draft, Phase 0 about to start build

> **Name**
> Mycelium — the underground fungal network that routes resources between trees across a forest with no central trunk. Fits the shape of the product: independently owned nodes, connected through a coordinator, growing from a small trusted pool into a public network over time.

> **Relationship to Bailment**
> Mycelium started as a brainstorm inside the sibling `Bailment` repo before moving to its own repo. Bailment's thesis ("ship tasks, not tokens") is explicitly the opposite of what Mycelium does (move model weights/activations/tokens between hosts) — the two should not be merged or made to share architecture.

---

## 1. Problem Statement

Running inference on a capable LLM requires GPU hardware that most individuals and small labs don't own outright, while many people *do* have occasional access to idle or underused GPU capacity — a lab workstation, a campus HPC allocation, a home rig — that sits unused most of the time. There's no lightweight way to pool that scattered, occasional capacity into something that behaves like a single inference service, the way BitTorrent pools scattered bandwidth/storage or BOINC pools scattered compute cycles.

## 2. Solution

Mycelium lets a user opt a GPU machine in as a **node**: it registers with a coordinator, hosts an LLM, and serves inference requests routed to it. A client talks to one stable endpoint (the coordinator) without needing to know which physical machine actually runs the model. The system is built in phases, each one a strict superset of the last, so the hard problems (public trust, multi-model orchestration, model parallelism) are deferred until the basic mechanics are proven.

## 3. Phases

| Phase | Scope | Status |
|---|---|---|
| **0** | One LLM. A small, closed pool of nodes the operator personally controls (HPC allocations, VPN-gated lab machines). Prove client → coordinator → node → response round-trips end to end. | **Building now** |
| **1** | Open the pool to public, opt-in volunteer nodes — torrent/BOINC-style. Still one LLM per request; trust and abuse-prevention become real concerns. | Future |
| **2** | Multiple different LLMs hosted across different nodes, composed by an agent's multi-LLM flow. Context/prompt state gets passed between hosts as the agent moves between models. | Future |
| **3** | Split a single LLM's *layers* across multiple GPU farms (pipeline/model parallelism) for models too large for any one farm to hold; activations pass host to host mid-inference. | Far future |

Non-goals for *every* phase covered by this document: payments/incentive mechanisms, model training or fine-tuning, multi-tenant SLAs. These may become relevant far downstream but are not shaping any decision made here.

---

## 4. Phase 0 — Architecture

**Components:**

- **Coordinator** — a single, publicly-reachable service. Holds the registry of currently-online nodes and which model each is hosting; routes an incoming client request to a healthy node; is the one stable address a client ever talks to.
- **Node agent** — runs on each opted-in GPU machine. Registers with the coordinator, sends heartbeats, and manages a local inference engine serving one HF model. Takes inspiration from the gateway/serve split already prototyped in the operator's [Bhaskera](https://github.com/dcll-iiitd/Bhaskera/tree/serve) framework, wrapping vLLM (optionally Ray-orchestrated, matching that prior work) rather than reimplementing serving from scratch.
- **Client** — sends a request to the coordinator and gets a response back, unaware of which node handled it.

**Data flow:** `Client → Coordinator (picks a healthy node hosting the requested model) → Node agent → vLLM → response → back up the chain`

**Coordination model:** centralized, not peer-to-peer. With a handful of trusted nodes, a single coordinator is simplest to build and debug; decentralized discovery is deferred to Phase 1, where "opt-in from strangers" makes a single trust anchor less appropriate.

**Failure handling:** if no healthy node is hosting the requested model, the coordinator fails the request immediately with a clear error. No queueing or retry logic in Phase 0.

**In scope for Phase 0:**
- Node registration + heartbeat/health-check against the coordinator
- Routing a single request to a single healthy node
- One HF model, chosen to fit comfortably on the smallest node's VRAM (exact model TBD against real hardware specs, not fixed here)
- A basic client interface to send a prompt and get a completion back

**Explicitly out of scope for Phase 0:**
- Public/untrusted node onboarding, sandboxing, or output verification
- Multiple simultaneous models or multi-node load balancing
- Any cross-host context or activation passing
- Queueing, retries, or failover

---

## 5. Phase 0 — Success Criteria

Phase 0 is done when:
1. Two or more real nodes (the operator's HPC/VPN-gated machines) register and heartbeat successfully with the coordinator.
2. A client sends one prompt; it round-trips correctly through coordinator → node → vLLM → response.
3. Killing a node removes it from routing — no request gets sent to a dead node.
4. A request made when no node is healthy fails with a clear, immediate error rather than hanging.

---

## 6. Representative User Stories

1. As a node operator, I want to opt a GPU machine into the pool with minimal setup, so that I can contribute idle capacity without babysitting it.
2. As a node operator, I want the coordinator to stop routing to my machine automatically if it goes offline, so that clients never hit a dead node.
3. As a client, I want to send a prompt to one stable address and get a completion back, so that I don't need to track which physical machine is currently serving the model.
4. As the operator, I want a clear failure when no node is available, so that I can tell "system down" apart from "request took forever."
5. As the operator, I want Phase 0's architecture to not paint Phase 1 (public nodes) into a corner, so that opening the pool later doesn't require rearchitecting the coordinator/node split.

*(Kept short deliberately — see note to reviewer at the end of this document.)*

---

## 7. Open Risks / Unresolved Decisions

- **Node network reachability is unconfirmed.** The operator's candidate machines sit behind a CDAC HPC environment and a university VPN, which likely means no inbound connectivity to them. If so, the coordinator cannot dial into a node — the node would need to dial out and hold a connection open instead (the approach BOINC/ngrok/torrent trackers use). This is **not yet designed for**; it needs to be confirmed against the real hardware before the coordinator↔node transport is finalized.
- **Node auth mechanism is a placeholder.** A shared pre-issued token per node is the working assumption for Phase 0's closed pool, but this hasn't been locked in.
- **Exact model choice is pending real hardware specs** for the HPC/VPN nodes the operator will use.

## 8. Prior Art

Worth being aware of, not treated as a dependency: **Petals** (BigScience) has already built public, volunteer-run layer-split inference for large models — directly relevant to Phase 3. **Hivemind**, also BigScience, is a DHT-based library for decentralized coordination among untrusted peers — relevant to Phase 1's move away from a single coordinator. Neither is assumed as a dependency here; noted so Phase 1/3 design doesn't reinvent solved problems unknowingly.

---

**Note to reviewer:** This PRD intentionally stops at overview level per the operator's request — it is not an implementation plan, exhaustive user-story catalog, or task breakdown. Those come later, scoped to Phase 0 only, once this document is approved.
