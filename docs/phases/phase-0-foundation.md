# Phase 0 — Foundation

Status: **Building now**
Depends on: nothing (first phase)
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md)

## Goal

Prove the basic loop works end to end: a client sends a prompt, it reaches
one of a handful of GPU machines the operator personally controls, and a
response comes back — before any of the hard distributed-systems problems
(public trust, multiple models, model parallelism) are in scope.

## Scope

One LLM. A small, closed pool of nodes the operator personally controls
(HPC allocations, VPN-gated lab machines).

## Architecture

**Components:**

- **Coordinator** — a single, publicly-reachable service. Holds the registry of currently-online nodes and which model each is hosting; routes an incoming client request to a healthy node; is the one stable address a client ever talks to.
- **Node agent** — runs on each opted-in GPU machine. Registers with the coordinator, sends heartbeats, and manages a local inference engine serving one HF model. Takes inspiration from the gateway/serve split already prototyped in the operator's [Bhaskera](https://github.com/dcll-iiitd/Bhaskera/tree/serve) framework, wrapping vLLM (optionally Ray-orchestrated, matching that prior work) rather than reimplementing serving from scratch.
- **Client** — sends a request to the coordinator and gets a response back, unaware of which node handled it.

**Data flow:** `Client → Coordinator (picks a healthy node hosting the requested model) → Node agent → vLLM → response → back up the chain`

**Coordination model:** centralized, not peer-to-peer. With a handful of trusted nodes, a single coordinator is simplest to build and debug; decentralized discovery is deferred to Phase 1, where "opt-in from strangers" makes a single trust anchor less appropriate.

**Failure handling:** if no healthy node is hosting the requested model, the coordinator fails the request immediately with a clear error. No queueing or retry logic in Phase 0.

## In scope

- Node registration + heartbeat/health-check against the coordinator
- Routing a single request to a single healthy node
- One HF model, chosen to fit comfortably on the smallest node's VRAM (exact model TBD against real hardware specs, not fixed here)
- A basic client interface to send a prompt and get a completion back

## Explicitly out of scope

- Public/untrusted node onboarding, sandboxing, or output verification
- Multiple simultaneous models or multi-node load balancing
- Any cross-host context or activation passing
- Queueing, retries, or failover

## Success criteria

Phase 0 is done when:
1. Two or more real nodes (the operator's HPC/VPN-gated machines) register and heartbeat successfully with the coordinator.
2. A client sends one prompt; it round-trips correctly through coordinator → node → vLLM → response.
3. Killing a node removes it from routing — no request gets sent to a dead node.
4. A request made when no node is healthy fails with a clear, immediate error rather than hanging.

## User stories

1. As a node operator, I want to opt a GPU machine into the pool with minimal setup, so that I can contribute idle capacity without babysitting it.
2. As a node operator, I want the coordinator to stop routing to my machine automatically if it goes offline, so that clients never hit a dead node.
3. As a client, I want to send a prompt to one stable address and get a completion back, so that I don't need to track which physical machine is currently serving the model.
4. As the operator, I want a clear failure when no node is available, so that I can tell "system down" apart from "request took forever."
5. As the operator, I want Phase 0's architecture to not paint Phase 1 (public nodes) into a corner, so that opening the pool later doesn't require rearchitecting the coordinator/node split.

## Open risks / unresolved decisions

- **Node network reachability is unconfirmed.** The operator's candidate machines sit behind a CDAC HPC environment and a university VPN, which likely means no inbound connectivity to them. If so, the coordinator cannot dial into a node — the node would need to dial out and hold a connection open instead (the approach BOINC/ngrok/torrent trackers use). This is **not yet designed for**; it needs to be confirmed against the real hardware before the coordinator↔node transport is finalized.
- **Node auth mechanism is a placeholder.** A shared pre-issued token per node is the working assumption for Phase 0's closed pool, but this hasn't been locked in.
- **Exact model choice is pending real hardware specs** for the HPC/VPN nodes the operator will use.

## Next step

Once this document and the risks above are settled, write a Phase 0 implementation plan (not yet started).
