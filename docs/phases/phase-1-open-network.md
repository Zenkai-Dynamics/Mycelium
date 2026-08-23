# Phase 1 — Open Network

Status: Brainstormed and PRD published ([issue #31](https://github.com/Zenkai-Dynamics/Mycelium/issues/31)) — not yet implemented
Depends on: [Phase 0](phase-0-foundation.md) working end to end
Related: [Phase 1 design rationale](../superpowers/specs/2026-08-23-phase-1-open-network-design.md)

## Goal

Open the node pool from "machines the operator personally controls" to
public, opt-in volunteers — torrent/BOINC-style — while still serving one
LLM per request. This is the phase where trust stops being assumed.

## What's decided

- Still single-model-per-request serving — no new capability over Phase 0,
  just a much larger and untrusted set of nodes.
- Anyone can opt a GPU in, the way anyone can seed a torrent or donate
  cycles to BOINC/Folding@home.
- Trust and abuse-prevention become real, load-bearing concerns — this is
  the explicit reason this phase is separated from Phase 0 rather than
  building the public network from day one.
- No payment/incentive mechanism (global non-goal — see Readme). Whatever
  keeps volunteers honest and participating, it isn't money, at least not
  in scope here.
- **Discovery: stays centralized.** The coordinator remains the single
  trust anchor and routing authority, unchanged from Phase 0.
  Decentralized discovery (DHT/tracker-style) is pushed to Phase 2/3
  territory or a later revisit — trust and discovery topology are
  orthogonal problems, and solving both at once was rejected as doubling
  the hard problems in one phase.
- **Client access: unchanged from Phase 0.** Only the node side opens to
  volunteers; clients still authenticate with a single shared operator
  token. Opening client-side access too was rejected as a separate,
  unscoped problem (request quotas, rate-limiting, cost control for
  anonymous public clients).
- **Node authentication: self-generated keypair + GitHub OAuth device-flow
  binding.** A node generates its own keypair on first run (the public key
  is its identity — no coordinator-issued secret per node); the coordinator
  additionally requires a one-time GitHub OAuth device-flow sign-in
  (headless-friendly, no local browser/callback needed) to bind that
  identity to something with real-world accountability.
- **Sybil resistance: a simple per-identity cap** on how many node public
  keys one bound GitHub identity can register at once (proposed default:
  3, tunable). Heavier Sybil-resistance machinery (proof-of-work, deeper
  verification) was rejected as premature.
- **Abuse prevention / output verification: deferred entirely.** Only
  protocol-level health (timeouts, crashes, malformed responses) is
  checked, same as Phase 0 — never response *content*. LLM sampling is
  stochastic, so redundant-computation-style validation doesn't map
  cleanly onto it the way it does for deterministic compute.
- **Client prompt privacy: documented, accepted risk.** A serving node
  necessarily sees the plaintext prompt — no technical mitigation is
  feasible without breaking inference itself. Disclosed plainly, matching
  Folding@home's work-unit transparency model.
- **Reputation: tracked, used as a soft routing preference.** Per-node
  completion/timeout/crash/disconnect counters (from signals the
  coordinator already produces) softly weight round-robin selection —
  still round-robin among healthy nodes, not a hard cutoff.
- **Manual ban** is an operator override, backstopping cases reputation
  signals don't catch. No automated appeals flow.
- **Opt-in mechanics: CLI-only, self-service.** `mycelium-node` drives the
  GitHub device-flow sign-in itself on first run — no separate signup
  website or new architectural component.

See [the design doc](../superpowers/specs/2026-08-23-phase-1-open-network-design.md)
for the full reasoning behind each decision above, and
[issue #31](https://github.com/Zenkai-Dynamics/Mycelium/issues/31) for the
PRD (problem statement, user stories, implementation/testing decisions,
explicit out-of-scope list).

## Non-goals (inherited)

Payments/incentive mechanisms, model training/fine-tuning, multi-tenant
SLAs — see Readme §3.
