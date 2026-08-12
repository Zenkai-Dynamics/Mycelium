# Phase 1 — Open Network

Status: Future — not started, not brainstormed in depth yet
Depends on: [Phase 0](phase-0-foundation.md) working end to end

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

## Open questions — needs its own brainstorming pass before building

Nothing below has been decided. These are the questions Phase 0's design
explicitly deferred rather than answered:

- **Discovery mechanism.** Does the Phase 0 centralized coordinator scale
  to a public network, or does this phase need to move toward
  decentralized discovery (DHT/tracker-style, as in BitTorrent or the
  `Hivemind` library — see Readme's Prior Art)? Phase 0's design chose
  "centralized for now, revisit when it matters" — this is where it
  matters.
- **Node authentication.** Phase 0's shared-token-per-node approach
  assumes the operator personally issues every token. That doesn't work
  once anyone can join — what replaces it?
- **Abuse prevention / output verification.** A malicious or broken node
  could return wrong, corrupted, or harmful output. Is that detected? How?
- **Sybil resistance.** With no payment barrier to entry, what stops
  someone registering many fake or malicious nodes?
- **Client prompt privacy.** Client prompts now reach a machine the
  operator does not control or trust. Is that acceptable, disclosed,
  mitigated? Not analyzed yet.
- **Reputation.** Does a node's track record affect whether it gets
  routed to? Not decided either way.
- **Practical opt-in mechanics.** How does a volunteer actually join —
  download a node-agent binary, a sign-up page, something else? Not
  designed.

## Non-goals (inherited)

Payments/incentive mechanisms, model training/fine-tuning, multi-tenant
SLAs — see Readme §3.
