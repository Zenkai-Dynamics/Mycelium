# Issue #8 — Node Registration Handshake — Design

Date: 2026-08-15
Status: Approved, not yet implemented
Issue: [#8 — Node registration handshake](https://github.com/Zenkai-Dynamics/Mycelium/issues/8)

This is the condensed record of the decisions made while brainstorming/
grilling issue #8, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established
in the [issue #1](2026-08-12-issue-1-project-skeleton-design.md),
[issue #2](2026-08-14-issue-2-dependency-pinning-design.md),
[issue #3](2026-08-14-issue-3-developer-setup-guide-design.md),
[issue #4](2026-08-14-issue-4-node-network-reachability-design.md),
[issue #5](2026-08-14-issue-5-coordinator-node-transport-design.md),
[issue #6](2026-08-14-issue-6-validate-model-vllm-design.md), and
[issue #7](2026-08-14-issue-7-node-agent-vllm-wrapper-design.md) design
docs.

## What issue #8 asks for

A node agent registers itself with the coordinator over the transport
built in #5, authenticating with the shared pre-issued token (the working
placeholder from
[the Phase 0 design doc](2026-08-12-mycelium-phase0-design.md)). Once
registered, the coordinator knows the node exists and which model it
hosts. Acceptance criteria: the node agent sends a registration message
on startup including its token and hosted model; the coordinator
validates the token and rejects registration with an invalid or missing
one; a registered node appears in a coordinator-side registry queryable
by an operator (e.g. a simple list/status command).

`connection.py` and `server.py` (#5) deliberately carry zero business
logic today — both modules' docstrings explicitly draw the line at "that's
issue #8." Heartbeat/liveness tracking beyond initial registration (#9)
and routing a client request to a registered node (#10) are both out of
scope here.

## Decisions made

**Token: one shared secret for all nodes, not a distinct token per
node.** `docs/superpowers/specs/2026-08-12-mycelium-phase0-design.md`'s
"a shared pre-issued token per node" phrasing is genuinely ambiguous
between these two readings — resolved here in favor of a single value
the coordinator is configured with and every node presents. In Phase 0's
closed pool the operator personally controls and trusts every node
equally; a per-node token would buy per-node revocation/identification
that nothing in Phase 0 needs yet, at the cost of the operator generating
and distributing N secrets instead of one. Node identity (see below) is
handled separately from the token, so this doesn't block distinguishing
nodes in the registry.

**Token delivery: `--token-file`, not a bare `--token` flag or an
environment variable.** A CLI flag would be visible in `ps aux` output
and persist in shell history — a real leak for a credential, even in a
closed-pool trust model. `--token-file` matches this codebase's existing
pattern for secrets (`--coordinator-cert`, `--cert-file`/`--key-file` are
all file paths, never inline values) rather than introducing a second,
inconsistent convention. Both `mycelium-node` and `mycelium-coordinator`
gain this flag; the file contains exactly one token string.

**Registration protocol: an application-level JSON message after the
WebSocket connects, not a handshake-layer header.** Considered
authenticating via a custom header during the WebSocket handshake
(`websockets`' `additional_headers`) — rejected because #5's `server.py`
docstring already draws the "no business logic in the transport" line
at the connection layer, and a handshake-level rejection can't cleanly
carry a structured reason back to the node before closing, which the
next decision requires. Messages, in order:

- Node → coordinator: `{"type": "register", "token": "...", "model": "...", "node_id": "..."}`
- Coordinator → node, success: `{"type": "registered"}`
- Coordinator → node, failure: `{"type": "registration_rejected", "reason": "..."}`, then the coordinator closes the connection

**Explicit success ack, not silence-means-success.** Considered treating
"the connection stays open" as sufficient success signal (symmetric with
how #5 already works, and one fewer message type) — rejected in favor of
an explicit `{"type": "registered"}` ack, symmetric with the rejection
message, and because #9's heartbeat loop needs an unambiguous point to
start from rather than inferring "safe to begin heartbeating" from
silence.

**Registration acknowledgment is timeout-bounded on both sides, matching
this project's established fail-fast principle** (`docs/phases/
phase-0-foundation.md`'s "fail fast" failure handling, #7's
`wait_ready` timeout). Node side: `mycelium-node` gives up waiting for
`registered`/`registration_rejected` after a bounded timeout and falls
back to its existing reconnect-backoff loop (#5) rather than hanging
forever on a coordinator that accepted the connection but never
responded. Coordinator side: a connection that never sends a
registration message within a bounded timeout is closed, rather than
held open indefinitely — cheap insurance against a stalled or buggy
client tying up a connection slot, and directly relevant once Phase 1
opens the pool to less-trusted connections.

**Node identity: self-reported `--node-id`, defaulting to the machine's
hostname.** The registry needs to show an operator something meaningful
per node, not just an anonymous connection count. `mycelium-node` gains
`--node-id`, sent in the registration message; if omitted, it defaults
to `socket.gethostname()` so a zero-config run still produces a sane
name (e.g. `a6000`) rather than a generated ID.

**Duplicate `node_id`: replace the old registry entry and actively close
its superseded connection.** If a registration arrives for a `node_id`
already in the registry — most commonly a node agent reconnecting after
a network blip or restart, racing its own stale entry's cleanup —
rejecting it as a conflict would fight the reconnect-with-backoff
behavior #5 already built. The coordinator replaces the entry and closes
the old WebSocket explicitly, rather than leaving it to time out on its
own via ping/pong — avoiding a leaked duplicate connection consuming a
server task for no purpose, and removing any ambiguity about which of
two live connections is "the real" one for that `node_id`. A genuine
misconfiguration (two physical nodes sharing an ID) surfaces to the
operator as the registry only ever showing one entry, not as a
coordinator-side rejection.

**Disconnect: remove the registry entry immediately, no retained
history.** When a node's connection closes for any reason, its entry is
dropped right away. Considered marking entries "disconnected" and
retaining them for operator visibility into recent history — rejected as
scope creep into #9's territory, which already owns liveness tracking and
a "bounded, documented time window" for dropping dead nodes; #8 stays
scoped to current membership only.

**Registry query: a new script, `mycelium-coordinator-status`, reusing
the existing WebSocket port and the shared token — not a new HTTP
endpoint, not subcommands, not log-only.** The acceptance criteria wants
the registry "queryable by an operator (e.g. a simple list/status
command)," but the coordinator has no HTTP surface — #5's design
explicitly deferred introducing an ASGI framework until a real
client-facing route needs it (#10), and running a second server stack
(a stdlib `http.server` alongside the async `websockets` server) for a
read-only status check isn't proportionate. Instead,
`mycelium-coordinator-status` connects exactly like a node would (same
TLS cert, same `--token-file`) and sends a distinct message type; the
coordinator responds with the current registry as JSON and closes. Zero
new ports, zero new dependencies, zero new auth concepts — reuses #5's
transport entirely. The status query authenticates with the same shared
token nodes use, rather than a separate operator-only credential:
Phase 0's operator personally issued that token to every node already,
so they hold it; a second credential is real setup/sync overhead for a
node-vs-operator distinction that doesn't otherwise exist yet.
`mycelium-coordinator-status` is a new script rather than a
`mycelium-coordinator status` subcommand, so `mycelium-coordinator`'s
existing invocation shape (already shipped and documented in
`docs/SETUP.md`) doesn't change.

**Module layout.** `mycelium/node/registration.py` (new) — builds and
sends the registration message, awaits the ack/rejection with a timeout.
`connection.py` stays transport-only, exactly as its docstring already
promises. `mycelium/coordinator/registry.py` (new) — the in-memory
registry (register/replace/unregister, token check). `server.py`'s
`_handle_node` gains JSON message parsing and dispatch on `"type"`
(currently just loops reading and discarding messages).
`mycelium/coordinator/status_cli.py` (new) — the
`mycelium-coordinator-status` entry point.

## Live verification

Run for real against the `a6000` node (`training-framework@192.168.22.23`)
after implementation, a final whole-branch review, and one approved
addendum fix (see below) were all complete. Both the coordinator and the
node ran on `a6000` itself — #5 already proved real cross-machine dial-out
connectivity separately, so this verification is scoped to what #8 adds
on top of that already-proven transport: the registration protocol
itself, not network reachability again.

A real random token (`openssl rand -hex 32`) was written to a token file;
`mycelium-coordinator` started against it. `mycelium-node --gpu 2` (GPU 2
was idle at verification time — GPUs 0/1/3 were carrying other users'
workloads on this shared machine) started a real `vllm serve
Qwen/Qwen2.5-7B-Instruct` and registered:

```
$ mycelium-node --coordinator-url wss://127.0.0.1:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt \
    --node-id a6000-live-verify --gpu 2
[...vLLM startup...]
vLLM ready
mycelium-node 0.1.0 connecting to wss://127.0.0.1:8765
connected to coordinator (wss://127.0.0.1:8765)
registered with coordinator as 'a6000-live-verify'

$ mycelium-coordinator-status --coordinator-url wss://127.0.0.1:8765 \
    --coordinator-cert coord-cert.pem --token-file token.txt
a6000-live-verify: Qwen/Qwen2.5-7B-Instruct
```

This closes acceptance criteria 1 and 3. Criterion 2 (invalid/missing
token rejected) was verified two ways against the same live coordinator:
`mycelium-coordinator-status` with a wrong token file printed `error:
coordinator rejected the status query (check --token-file)` and exited
non-zero, rather than a raw traceback; and a raw registration message
with an intentionally wrong token, sent directly against the real running
coordinator, returned `{"type": "registration_rejected", "reason":
"invalid or missing token"}` — and the imposter node never appeared in a
follow-up status query, confirming rejection happens before any registry
mutation, not just cosmetically at the response level.

Finally, `SIGTERM` to the real node process — combining issue #7's
signal-handling fix with #8's registration — confirmed the full stack
shuts down cleanly: the node process exited, `vllm serve` and its
process group were gone (`nvidia-smi` back to GPU 2's 4 MiB idle
baseline), and a follow-up status query showed `No nodes registered.`,
confirming the coordinator's disconnect-cleanup path fired for real, not
just in the local test suite's simulated disconnects.

**Addendum fix, found and fixed after the final review, before this live
run:** the final review's own fix-wave re-review independently discovered
a real, pre-existing bug — a node retrying registration after a failure
on an *already-open* connection (wrong token, or the coordinator closing
mid-handshake) had no backoff, because `websockets`' reconnect backoff
only applies between failed TCP connections, not failed registrations on
a successful one; verified at ~400-420 attempts/second sustained. Fixed
by reusing `connection.reconnect_delays()` for registration failures too,
resetting after a successful registration. The user was consulted and
explicitly chose to fix this in the same PR rather than file a follow-up
issue, given the live-verification step right after it would otherwise
have hammered the coordinator during exactly this kind of test.

## Explicitly out of scope for this issue

Heartbeat/liveness tracking after registration succeeds, and dropping a
node whose connection is still open but has gone quiet (#9). Routing a
client request to a registered node (#10). Per-node tokens or any
token-revocation mechanism (the single-shared-token model is Phase 0's
deliberate baseline, not a placeholder this issue needs to firm up
further). Retaining disconnected nodes' history in the registry. Any new
HTTP/ASGI surface on the coordinator. Authenticating at the TLS/WebSocket
handshake layer instead of via an application-level message.
