# Issue #5 — Coordinator↔Node Transport — Design

Date: 2026-08-14
Status: Approved, not yet implemented
Issue: [#5 — Implement the coordinator↔node transport](https://github.com/Zenkai-Dynamics/Mycelium/issues/5)

This is the condensed record of the decisions made while brainstorming/
grilling issue #5, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established
in the [issue #1](2026-08-12-issue-1-project-skeleton-design.md),
[issue #2](2026-08-14-issue-2-dependency-pinning-design.md),
[issue #3](2026-08-14-issue-3-developer-setup-guide-design.md), and
[issue #4](2026-08-14-issue-4-node-network-reachability-design.md)
design docs.

## What issue #5 asks for

The actual connection mechanism between a node and the coordinator, using
the dial-out direction decided in [ADR-0002](../adr/0002-node-transport-model.md)
(issue #4). A node process establishes and holds a connection to a
coordinator process running on a separate, publicly-reachable host, and
reconnects if the connection drops. Acceptance criteria: tested across
two real machines (not localhost), survives being idle for a few
minutes, auto-reconnects on drop, and the direction matches ADR-0002.

## Decisions made

**Transport: WebSocket (`wss://`), via the `websockets` library.**
Considered gRPC bidirectional streaming and a reverse SSH tunnel as
alternatives. Rejected gRPC for Phase 0: its raw HTTP/2 framing is
historically less reliable through the kind of institutional
firewalls/NATs [issue #4](2026-08-14-issue-4-node-network-reachability-design.md)
already showed behave unpredictably (`paramrudra`'s firewall silently
dropped a non-SSH port rather than rejecting it cleanly), whereas
WebSocket traffic looks like ordinary HTTPS to anything in between.
Rejected reverse SSH tunneling as heavier and more sysadmin-flavored than
a purpose-built protocol — it would still need an application-level
message protocol layered on top to distinguish message types, so it
doesn't actually save complexity. Pinning `websockets==17.0.1` (latest
stable as of 2026-08-14, verified against PyPI directly, matching the
rigor [issue #2](2026-08-14-issue-2-dependency-pinning-design.md)
established for dependency pinning).

**Coordinator: a bare `websockets` server, not an ASGI framework.**
FastAPI/Starlette was considered, anticipating that issue #10 (routing
client requests) will eventually need HTTP endpoints too. Rejected for
now — YAGNI: #5 only needs a node-facing socket, and introducing an ASGI
framework before anything exercises HTTP routes is speculative
complexity. #10 can introduce it then, once there's a concrete route to
build.

**TLS: self-signed cert, pinned by the node — not a real domain +
Let's Encrypt.** The coordinator has only a raw IP (`20.244.2.48`, an
Azure VM the operator provided), no domain name, so standard
CA-validated TLS isn't straightforward. The coordinator generates a
self-signed certificate (via the `cryptography` library, pinning
`cryptography==50.0.0`, latest stable as of 2026-08-14) with the
coordinator's IP as a Subject Alternative Name. That cert (public half
only) is distributed to each node out-of-band (e.g. `scp`'d by the
operator) and the node's `ssl.SSLContext` loads it as its sole trusted
issuer via `load_verify_locations` — the node trusts *exactly this
cert*, not a CA chain. TLS was chosen for issue #5 despite carrying no
real inference traffic yet, because it's the default mode for the
`websockets` library anyway (little extra work) and means issue #8's
auth token is never sent in the clear later — cheaper to build in now
than retrofit.

**Scope boundary: keepalive only, zero business logic.** Nothing flows
over the connection except WebSocket ping/pong frames. No node ID, no
auth token, no registration message — that's
[issue #8](https://github.com/Zenkai-Dynamics/Mycelium/issues/8)'s job.
#5 only has to prove the pipe connects, stays open, and self-heals.
Rejected sending a minimal node-identifying handshake now as a "head
start" on #8 — it would blur the line between what #5 and #8 each
deliver, and #8 isn't blocked on any code #5 would add for this.

**Keepalive mechanism: `websockets`' built-in ping/pong,
`ping_interval=20s`, `ping_timeout=20s`.** No custom keepalive protocol
needed — the library already sends control-frame pings automatically and
closes the connection if pong responses stop arriving. 20s is comfortably
shorter than the "few minutes" idle bar in the acceptance criteria,
leaving margin to actually prove the connection survives well past it,
not just barely.

**Reconnect: exponential backoff with jitter, node side.** Initial delay
1s, ×2 multiplier, capped at 30s, ±20% jitter, retries indefinitely (a
Phase 0 node process is expected to run long-lived and unattended, per
`docs/phases/phase-0-foundation.md`'s user story #1 — it shouldn't give
up). Jitter avoids every node reconnecting in lockstep after a shared
coordinator restart, though at Phase 0's node-pool scale this is a cheap
precaution more than a pressing need.

**CLIs gain real arguments, via stdlib `argparse`.** `mycelium-coordinator`
takes `--host`, `--port`, `--cert-file`, `--key-file` (auto-generates the
cert/key on first run if the files don't exist). `mycelium-node` takes
`--coordinator-url` and `--coordinator-cert`. `argparse` over `click`/
`typer`: no new dependency, and Phase 0 doesn't yet need either library's
extra ergonomics — this also sets the pattern future CLI work in this
repo follows unless a real need for more emerges.

**Test infrastructure.** Coordinator tested on a real Azure VM
(`20.244.2.48`, Ubuntu 24.04) the operator already had — confirmed
reachable via SSH, confirmed via a live test that only port 22 was open
inbound at the Azure network-security-group (NSG) level (a `python3 -m
http.server` test on port 8443 timed out from an external vantage before
the operator opened a wider port range). Node side tested from the real
`a6000` and `h100` machines (VPN-gated, private IPs) already used in
[issue #4](2026-08-14-issue-4-node-network-reachability-design.md)'s
investigation. `paramrudra` is explicitly excluded from this ticket's
live testing — the operator declined to spend GPU-partition SLURM
allocation time on it for this ticket (a live `srun`/`sbatch` request
against `paramrudra`'s real `gpu` partition, confirmed to exist with 30
nodes/2 GPUs each via `sinfo`, would have been needed to test from an
actual compute node rather than the login node ADR-0002 already flagged
as insufficient evidence). This means ADR-0002's "outbound egress from
node environments is unverified" open risk is **not** resolved by this
issue — it remains open, unchanged, for a separate later check.

## Explicitly out of scope for this issue

Node registration, auth-token validation, or any coordinator-side
registry of connected nodes queryable by an operator — all
[issue #8](https://github.com/Zenkai-Dynamics/Mycelium/issues/8).
Heartbeat/liveness semantics beyond the transport's own
keepalive — [issue #9](https://github.com/Zenkai-Dynamics/Mycelium/issues/9).
Routing an actual client request over this connection —
[issue #10](https://github.com/Zenkai-Dynamics/Mycelium/issues/10).
Resolving `paramrudra`'s outbound-egress-from-a-compute-node open risk
(deliberately deferred, see above). Any client-facing HTTP surface on
the coordinator (no ASGI framework introduced yet, see above).
