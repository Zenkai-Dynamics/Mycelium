# Phase 1 — Open Network — Design

Date: 2026-08-23
Status: Approved, PRD published as issue #31, not yet implemented
Issue: [#31 — Phase 1: Open node pool to public volunteers](https://github.com/Zenkai-Dynamics/Mycelium/issues/31)

This is the condensed record of the decisions made while brainstorming
Phase 1, before the PRD was written. It exists so the *reasoning* behind
each decision isn't lost, per the pattern established in
[the Phase 0 design doc](2026-08-12-mycelium-phase0-design.md). Issue #31
(this doc's companion) is the *what*; this doc is the *why*.

## What Phase 1 asks for

`docs/phases/phase-1-open-network.md` scoped the goal (open the node pool
to public, opt-in volunteers, torrent/BOINC-style, still one model per
request) and listed seven genuinely open questions Phase 0 deliberately
deferred: discovery mechanism, node authentication, abuse prevention/
output verification, Sybil resistance, client prompt privacy,
reputation, and practical opt-in mechanics. None of them were decided —
this brainstorming session decided all seven, plus two more that
surfaced along the way (client access model, identity-binding
mechanism).

## Decisions made

**Discovery stays centralized.** The coordinator remains the single
trust anchor and routing authority, unchanged from Phase 0. Rejected
moving toward decentralized (DHT/tracker-style) discovery in this phase:
Phase 1's actual new problem is *trust* (volunteers, not the operator,
run the nodes) — an orthogonal axis from *discovery topology*. Solving
both in one phase roughly doubles the hard problems at once, and a
centralized coordinator is a *better* place to do abuse prevention,
rate-limiting, and reputation tracking than a DHT is, not a worse one.
Decentralized discovery is pushed to Phase 2/3 territory or a later
revisit.

**Client access stays exactly as Phase 0 left it.** Only the node side
opens to volunteers. Opening client-side access too would mean solving
request-side quotas/rate-limiting/cost-control for anonymous public
clients — a whole separate problem Phase 1's own doc never scoped.
Keeps this phase focused on the trust problem it was actually framed
around, rather than quietly also turning Mycelium into a public
inference service.

**Node authentication: self-generated keypair identity + GitHub OAuth
device-flow binding.** Phase 0's model (operator personally issues every
token via `--token-file`) assumed the operator trusts and has
provisioned every node — that doesn't scale once anyone can join. A node
now generates its own keypair on first run; the public key *is* its
identity, with no coordinator-issued secret to distribute or leak per
node. That alone would allow fully anonymous, zero-friction registration
(pure BitTorrent-style) — rejected in favor of binding that identity to
something with a little real-world weight behind it: GitHub OAuth,
specifically the *device flow* (the same mechanism `gh`/`docker login`
use — show a code, the volunteer enters it at `github.com/login/device`
on any device with a browser). Chosen over email verification because it
works cleanly on headless HPC/server nodes without a local browser or
callback listener, and a GitHub account is a materially better
accountability proxy than a freely-creatable email address, at no extra
infrastructure cost to the coordinator (no SMTP/transactional-email
integration needed — the node talks to GitHub directly).

**Sybil resistance: a simple per-identity cap, not a heavier system.**
GitHub OAuth binding alone doesn't stop someone creating multiple
throwaway GitHub accounts. Rather than building real Sybil-resistance
machinery (proof-of-work registration, deeper identity verification) —
premature for a first cut, and there's no payment mechanism creating a
strong incentive to Sybil-attack in the first place — the coordinator
caps how many node public keys can map to one bound GitHub identity at
once (proposed default: 3, a tunable config value, not an architectural
commitment). Cheap to implement, closes the obvious "one signup
registers 500 fake nodes" loophole without new infrastructure.

**Abuse prevention / output verification: deferred entirely.** Phase 1
checks protocol-level health only (timeouts, crashes, malformed
responses — identical to Phase 0), never the *content* of a node's
response. Rejected redundant-execution/consensus and spot-check
verification: LLM sampling is inherently stochastic, so "compare two
nodes' output for the same prompt" isn't a clean signal the way
deterministic BOINC-style compute validation is — two honest nodes can
legitimately disagree token-for-token on a sampled completion. Content
correctness is documented as a known, disclosed Phase 1 limitation
rather than half-solved with a mechanism that doesn't actually fit the
problem.

**Client prompt privacy: documented, accepted risk.** A volunteer node
necessarily sees the plaintext prompt it serves — there's no way around
this that still lets the node run inference, so encryption schemes that
hide the prompt from the node itself aren't technically feasible.
Rejected building an opt-in "trusted node" routing tier for
privacy-sensitive requests as unnecessary scope for a first cut; Phase 1
instead documents the risk plainly, matching the transparency model
Folding@home already uses for its work units.

**Reputation: track protocol-level reliability, use it as a soft
routing preference.** Per-node counters for completions / timeouts /
crashes / disconnects, incremented at exactly the points
`NodeTimeoutError` / `NodeError` / `NodeDisconnectedError` are already
raised (#10/#11) — these signals are already produced "for free" by
existing code, nothing new to instrument. `find_node_for_model`'s
round-robin gets a soft weighting pass using these counters — still
round-robin among healthy nodes, not a hard reliability cutoff, so a
node with a rough track record can still be picked, just less
preferentially. Rejected fully deferring reputation to a later phase:
this is a meaningfully cheap addition that improves the "opening to
strangers" trust story without needing the harder content-verification
problem above.

**Manual ban as an operator override.** A new operator-facing command,
sibling to `mycelium-coordinator-status`, lets the operator revoke a
specific node identity outright — the backstop for cases reputation
signals don't catch (e.g. a report of bad-faith output that never trips
a timeout or crash). No automated appeals/dispute flow in this phase;
banning is a manual operator action.

**Opt-in mechanics: CLI-only, self-service.** `mycelium-node` itself
drives the GitHub device-flow dance on first run — no separate signup
website or new web-service component. Keeps the architecture to the
same three pieces Phase 0 already has (coordinator, node, client), and
matches Phase 0's own user story ("opt a GPU machine in with minimal
setup"). A separate signup page was considered and rejected as
unnecessary added surface area.

## Explicitly out of scope for Phase 1

Decentralized discovery (Phase 2/3 territory). Any verification of a
node's output content or correctness. Opening client-side access to the
public. Any technical mitigation for prompt privacy beyond disclosure.
Payments/incentive mechanisms (inherited global non-goal). An automated
appeals process for a manual ban. Non-GitHub identity providers.
