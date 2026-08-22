# Issue #33 — Node Self-Generated Keypair Identity (Wire Protocol) — Design

Date: 2026-08-23
Status: Implemented
Issue: [#33 — Node self-generated keypair identity (wire protocol)](https://github.com/Zenkai-Dynamics/Mycelium/issues/33)
Parent: [#31 — Phase 1: Open node pool to public volunteers](https://github.com/Zenkai-Dynamics/Mycelium/issues/31) —
see [the Phase 1 design doc](2026-08-23-phase-1-open-network-design.md) for
the full phase-level rationale this ticket implements one slice of.

This is the condensed record of the decisions made while brainstorming/
grilling issue #33, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
the Phase 0 issue design docs (issues #1 through #13, `docs/superpowers/
specs/2026-08-1*-issue-*-design.md`).

## What issue #33 asks for

A node generates and persists its own asymmetric keypair on first run — the
public key is the node's on-the-wire identity. The coordinator's
registration handling accepts a public key plus a signed proof of
possessing the matching private key, and recognizes a returning node by its
persisted public key across reconnects.

This ticket is wire-protocol/plumbing only — it does **not** change *who*
is allowed to register. Phase 0's shared token stays the admission gate,
unchanged, alongside the new identity layer. Swapping the token for real
GitHub-based trust is #39. Per-identity caps (#35), reputation (#36),
manual ban (#37), and the self-service device-flow CLI (#34) all build on
top of what this ticket establishes.

## Decisions made

**Algorithm: Ed25519.** Purpose-built for signing — small keys (32 bytes),
fast, deterministic signatures, no parameter choices to get wrong. The
coordinator's `cryptography==50.0.0` dependency (currently a
`coordinator`-extra, used today only for RSA-based TLS certs in
`certs.py`) supports it natively; it becomes a base dependency shared by
both `node` and `coordinator` installs rather than duplicated across
extras, since both sides now need it (node signs, coordinator verifies).
RSA-2048 (matching `certs.py`'s existing choice) was considered for "one
mental model for asymmetric crypto in this repo" — rejected as overkill
and slower for a pure-signing use case, with far larger keys/signatures to
carry in JSON messages for no benefit.

**What's signed: the public key bytes themselves, not a coordinator-issued
challenge.** The registration message carries the node's proof of
possession in the *same* single message as everything else — issue #33's
framing (`register` message extended, not a new handshake round-trip)
rules out a server-issued nonce/challenge, which would require the
coordinator to send something first. Signing the raw public-key bytes is a
minimal self-certifying statement ("this signature was produced by the
private key matching this public key"), the standard pattern for
self-sovereign key-possession proofs (SSH, WireGuard, age all work this
way — identity is a static keypair, not a session credential). Signing
`{node_id, model, public_key}` together was considered for extra binding —
rejected: it adds fields to canonicalize consistently, and buys no real
security margin here (an attacker who doesn't hold the private key can't
produce a valid signature over *anything*, and one who does hold it is the
legitimate node). Replaying a captured, verbatim registration message
isn't a new risk this design introduces — TLS already protects
confidentiality/integrity in transit, and a replay of a node's own valid
credentials is indistinguishable from that node reconnecting, which is
already anticipated, benign behavior.

**Wire encoding: base64.** `public_key` and `signature` are both raw
bytes; JSON only carries text. Base64 is roughly half the size of hex for
the same bytes and round-trips cleanly with Python's `base64` module and
JSON strings. Hex's only advantage (easier to eyeball-diff) doesn't matter
for opaque crypto material.

**Registry re-keys from `node_id` to the full public key.** Phase 0's
`NodeRegistry` is keyed by `node_id` — a free-text string the node itself
picks (defaults to hostname) — with "add or replace" semantics: whoever
registers under a given `node_id` silently supersedes whoever held it
before. That's fine in a trusted pool; it becomes a hijack vector once
strangers can connect; a volunteer could register under `node_id="gpu-1"`
and boot off whoever else was already using that label, no keypair
required. `NodeRegistry` re-keys on the node's full public key (as its
base64 string — unambiguous, zero collision risk). `node_id` stays a
required field (unchanged validation — no scope creep) but is demoted to a
display label with no security meaning; a duplicate `node_id` across two
different keypairs is no longer a conflict, since they're different
registry entries. Keying on a shortened fingerprint instead of the full
key string was considered for compactness — rejected: never let a
truncated value decide identity, only display it. `server.py`'s
`_handle_complete_request` failover loop (`tried`/`exclude` sets) moves
from `node_id` to the same public-key-based key.

**Display fingerprint: 12 hex chars of SHA-256 over the raw public key,
computed only at display time.** Used in `mycelium-coordinator-status`
output and logs (e.g. `node-a [a1b2c3d4e5f6]: model`) — never used as the
registry's actual key. Matches common short-hash conventions (git's
default abbrev length). `mycelium-coordinator-status` is touched in this
ticket rather than deferred to #39 (whose own acceptance criteria are
about the *GitHub-bound* identity, not the raw key) — `list_nodes()`'s
internal shape is already being touched by the registry restructuring, and
surfacing the fingerprint immediately makes the new identity layer visible
and debuggable rather than invisible until #39 lands.

**Persisted key file: PEM/PKCS8, path configurable via a new
`--node-key-file` flag.** PEM matches `certs.py`'s existing persisted-key
convention (openssl-inspectable, recognizable `BEGIN PRIVATE KEY`
boundary) rather than introducing a second, inconsistent raw-binary
convention. The path is **not** a single fixed default like
`~/.mycelium/node-key.pem` alone — `docs/OPERATIONS.md` already documents
running multiple nodes on one physical machine (distinct `--node-id`/
`--vllm-port` each); a fixed key path would make two co-located node
processes silently load the *same* keypair, which under the new
pubkey-keyed registry means they'd collide as one identity — the second's
registration superseding the first's, exactly the hijack this re-keying
exists to prevent, just moved one layer down. `--node-key-file` mirrors
the coordinator's existing `--cert-file`/`--key-file` override pattern,
defaulting to `~/.mycelium/node-key.pem`; co-located nodes are documented
(next to the existing `--vllm-port` caveat) as needing a distinct value
each, same as they already need a distinct `--vllm-port`. Auto-deriving
the path from `--node-id` instead was considered — rejected, since it ties
key identity to the label just demoted to "no security meaning," and would
silently break if two co-located nodes ever shared a `node_id` default
(hostname).

**Key generation: auto-generate on first run if the file is missing,
mirroring `certs.py`'s `ensure_cert`.** A corrupt or unreadable *existing*
file still fails loudly (same pattern as today's empty-`--token-file`
check) — only a missing file triggers generation. Requiring a separate
manual keygen step was considered — rejected, since it adds setup friction
Phase 0's quick-start flow doesn't currently have, and no ticket in the
Phase 1 set asks for a standalone keygen command.

**Rejection: reuses the existing `registration_rejected` message type**
with a new reason string for a bad or missing signature, rather than a new
message type — matches #35's own planned precedent (cap-rejection extends
`registration_rejected` rather than introducing a new type), and
tests/clients already know how to handle it.

**Check order: token → required-fields → signature**, appended to
`_handle_registration`'s existing early-return structure unchanged. An
unauthenticated probe (no valid token) never reaches the new signature
check, so it gets no signature-related error detail to fish for.

## Explicitly out of scope for this issue

*Who* is allowed to register — Phase 0's shared token remains the sole
admission gate; GitHub OAuth verification is #39. Per-identity registration
caps (#35). Reputation tracking/weighting (#36). Manual ban (#37). The
self-service device-flow sign-in CLI (#34) — this ticket assumes the
GitHub token (once #39 needs one) is supplied by some other means. Any
backward-compatible dual-mode registration for not-yet-upgraded nodes —
this is pre-1.0, single-operator-controlled software; every prior Phase 0
ticket shipped as a wholesale behavior change with no compatibility shim,
and this one follows the same pattern. Persisting node identity across
*coordinator* restarts — the registry stays in-memory only, exactly as
Phase 0 left it; a durable identity store is #39's concern (it needs one
for the GitHub-bound `identity_id` to mean anything across restarts), not
this ticket's.
