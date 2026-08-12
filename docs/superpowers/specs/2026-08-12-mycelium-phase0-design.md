# Mycelium Phase 0 — Design

Date: 2026-08-12
Status: Approved basis for Readme.md (PRD v0.1)

This is the condensed record of the decisions made during brainstorming,
before the PRD was written. See `Readme.md` for the product-facing version;
this doc exists so the *reasoning* behind each decision isn't lost.

## Decisions made

**Relationship to Bailment.** Brand new, separate project. Bailment's
thesis ("ship tasks, not tokens") is the opposite of what Mycelium does
(move weights/activations/tokens between hosts) — confirmed explicitly
rather than assumed, since both currently sit under the same parent
directory.

**Trust model, Phase 0.** Closed pool of nodes the operator personally
controls (HPC allocations, VPN-gated lab machines) — not an open public
network yet. Rejected doing the open-network trust/verification work now;
deferred to Phase 1. Rationale: prove the mechanics before defending
against adversarial hosts.

**Coordination model.** Centralized coordinator, not peer-to-peer/DHT,
for Phase 0. With a handful of trusted nodes a single coordinator is
simplest to build and debug. Peer-to-peer discovery is deferred to Phase 1,
where "opt-in from strangers" makes a single trust anchor insufficient
anyway — no benefit to building it early.

**Inference engine.** Wrap an existing serving engine (vLLM, optionally
Ray-orchestrated) rather than writing a custom inference runner. The
operator has prior working experience with a Ray + vLLM setup
([Bhaskera](https://github.com/dcll-iiitd/Bhaskera/tree/serve), a
training-focused framework with a `serve`/`gateway.py` split) — take
inspiration from that gateway/serve separation rather than depending on
that repo directly, since it's unfinished and training-focused.

**Failure handling.** Fail fast with a clear error when no healthy node
is available. No queueing/retry subsystem in Phase 0 — deliberately
deferred as unnecessary complexity for proving the happy path.

**Node auth.** Working assumption only: a shared pre-issued token per
node. Not firmly decided — see open risk below.

**Node connectivity — flagged as an open risk, not designed around.**
The operator's candidate nodes sit behind a CDAC HPC environment and a
university VPN, which likely means no inbound reachability. If so, the
coordinator cannot dial into a node; the node would need to dial out and
hold a connection open (the approach BOINC/ngrok/torrent trackers use).
Explicitly *not* architected for yet — needs to be confirmed against real
hardware first, rather than guessed at.

**Success bar for Phase 0.** End-to-end single request: ≥2 real nodes
register/heartbeat, one client request round-trips correctly, a dead node
gets removed from routing, and an unavailable-model request fails
cleanly. Rejected a higher bar (sustained multi-request reliability over
time) as premature for the first working version.

**Name.** Mycelium — see `docs/adr/0001-project-name.md`.

## Explicitly out of scope for Phase 0

Public/untrusted node onboarding, sandboxing, output verification,
multiple simultaneous models, multi-node load balancing, any cross-host
context/activation passing, queueing/retry/failover, payments or
incentive mechanisms, model training/fine-tuning, multi-tenant SLAs.
