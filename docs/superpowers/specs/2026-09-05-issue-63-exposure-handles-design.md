# Issue #63 — Exposure Handles and Per-Hop Timing — Design

Date: 2026-09-05
Status: Approved, not yet implemented
Related: [Phase 2 implementation design](2026-09-04-phase-2-implementation-design.md) (the cross-slice decisions this builds on),
[ADR-0004](../../adr/0004-explicit-per-hop-context.md),
[issue #63](https://github.com/Zenkai-Dynamics/Mycelium/issues/63),
[PRD: issue #54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54)

The Phase 2 implementation design settled *what* exposure handles are and
*why* they exist. This is the slice-level record: the decisions that only
came up once the code was in front of us. It supplements that document
rather than restating it — where the two overlap, that one is the
authority.

Phase 2 deliberately does not give each sub-issue its own design doc; the
cross-slice spec plus a plan per issue is the pattern. This one exists
because ten decisions came out of grilling that live nowhere else, and the
repo's convention is that specs are dated session records left as written
rather than edited later.

## Why this slice is separate from #59

The handle machinery is coordinator, router and registry work. It exists
only to serve #59's client-side exposure report, but it is independently
testable server-side with no client library at all — and keeping it here
stops #59's diff from spanning every component in the system. #59 is
blocked by both #55 (landed) and this.

## Decisions

### The primitive lives in `crypto.py`, the secret lives in the registry

`crypto.py` gains `handle(secret: bytes, value: str) -> str` — HMAC-SHA256,
hex, truncated. It is the sibling of the `fingerprint()` already there:
pure, and unit-testable without constructing a registry or opening a
socket.

`NodeRegistry` owns the secret and exposes one method that composes the
primitive with its own identity lookup:

```python
registry.handles_for(public_key) -> {"node_handle": str, "identity_handle": str | None}
```

`_identity_by_key` stays private. The alternative — a dedicated
`coordinator/handles.py` — reads better on paper but would require the
registry to expose bound identities to it, widening the visibility of the
GitHub-identity map to serve a reporting feature. This split matches how
the codebase already separates primitives from the state that uses them.

### Handles are 16 hex characters, deliberately not 12

`crypto.fingerprint()` truncates to 12. Handles use 16 so the two can never
be confused at a glance in a log line or a bug report. They are different
things with opposite disclosure properties: a fingerprint identifies a node
*to the operator*, and a handle exists precisely so a client cannot
identify anything. The failure mode worth engineering against is someone
reading a handle as a resolvable node identifier; different lengths make
that mistake visible. 64 bits is collision-free at any plausible size of
this network.

### The secret is injectable, defaulting to random

`NodeRegistry.__init__` takes `handle_secret: bytes | None = None`,
defaulting to `secrets.token_bytes(32)`; `serve()` passes it through. This
is the pattern the registry already uses for `identity_verifier`,
`per_identity_cap` and `random_source`, and for the same reason: tests can
inject a fixed secret and assert exact handle values, while production
callers never pass one and get the random default.

Deriving the secret from the shared client token was rejected outright:
clients hold that token, so they could recompute every handle and resolve
it straight back to a public key — defeating the whole mechanism.

### `elapsed_ms` spans the whole retry loop, not the winning attempt

The client computes overhead as `wall_time − elapsed_ms`. If `elapsed_ms`
covered only the successful attempt, time spent on a failed node would be
silently reattributed to client↔coordinator overhead — inflating the exact
number #61 exists to measure, and doing it invisibly, since the client
cannot tell a failover happened. Measured with `time.monotonic()` from the
start of the loop.

### Exposure means "the bytes left the coordinator for this node"

| Outcome | Exposed? |
|---|---|
| Send raised on an already-closed connection (`NodeSendFailedError`) | no — nothing reached the node |
| Send raised part-way through (`NodeDroppedError`) | yes — the frame may already have hit the transport |
| Dropped while awaiting reply (`NodeDroppedError`) | yes |
| Timed out | yes — the node received it and may still be processing |
| Node reported `complete_error` | yes |
| No healthy node | nothing was sent |
| Coordinator-side validation rejection | nothing was sent |

A timeout counting as exposure is the important one: a node that received
the array and never answered is indistinguishable from one that read it and
then failed. The report must assume the volunteer saw it.

`NodeDisconnectedError` splits into `NodeSendFailedError` and
`NodeDroppedError`, **both subclassing it**, so every existing catch site,
failover path and test is untouched. `_handle_registration`'s cleanup —
which sets `NodeDisconnectedError` on pending futures when a connection
dies mid-request — becomes `NodeDroppedError`, since that is exactly the
case where the node probably read the request.

Because failover can expose more than one node for a single hop, a hop's
exposure is a **list**, not a single pair.

Which of the two subclasses a failed send raises turns on the connection's
state sampled *immediately before* the send, not on the send raising.
`websockets` 17.0.1 (`asyncio/connection.py`, `Connection.send_context`)
writes nothing at all when the connection is not `OPEN` on entry — that
branch goes straight to raising `ConnectionClosed`. On an `OPEN`
connection it instead calls `send_data()`, handing the frame to the
transport, and only *then* awaits `drain()`; a failure during that drain
raises `ConnectionClosed` **after** the bytes went to the socket. So a
backpressured node whose connection dies during the drain may well have
received the conversation. Only the not-`OPEN` case can honestly claim
"nothing reached the node", so only it is `NodeSendFailedError`; a
mid-send failure is a `NodeDroppedError` and counts as exposure.

The residual inaccuracies, recorded rather than papered over, now all
point the same way: a send that succeeds locally but dies before
delivery, and a send that fails mid-flight having written nothing useful,
are both counted as exposure that may never have happened. The report
errs toward over-reporting, which is the right direction for a privacy
report to err — under-reporting would be a false privacy claim in the
flattering direction.

### A client-facing `reason` never names a node

`RoutingError` messages are relayed to the client verbatim as a reply's
`reason`, so none of them may carry `node_id` — which defaults to the
volunteer's `socket.gethostname()`. A timeout reply carries exactly one
entry in `exposed`, so a reason reading `node 'gpu-box.local' did not
respond` would hand the client the handle to machine-name mapping that
this whole slice exists to withhold, stable for the coordinator's
lifetime. The messages still say what went wrong; they just say it about
"the node" rather than about a named one.

### Field presence

`exposed` is on every `complete_result` and `complete_error`, `[]` when
nothing was sent — a uniform shape, so no client has to distinguish absent
from empty, which is a classic source of quiet bugs.

`elapsed_ms` is omitted entirely when no routing happened — precisely, when
the loop never picked a node to attempt. Reporting `0` would assert that
routing took no time rather than that it never occurred, and averaging
those zeros into #61's measurements would silently drag the numbers down.

"At least one node was attempted" is the condition, not "at least one node
was exposed". A request whose only candidate node turned out to have a dead
socket really did spend coordinator time before failing, and that time is
honest to report even though nothing reached a volunteer.

### A missing identity binding degrades, never fails

Every registered node should have a bound identity, since registration
always resolves one. If the lookup somehow misses, `identity_handle` is
`null` and the node handle still reports correctly. A completion must not
fail because a reporting field could not be built. The invariant is
documented in the code so a future reader knows `null` means "unexpectedly
unbound", not "anonymous node".

Falling back to hashing the public key was rejected: it would populate a
per-identity field with a per-node value, making two nodes under one owner
look like two separate people — the precise conclusion the report exists to
prevent.

### Testing an unfalsifiable criterion

The issue asks that handles "cannot be resolved to a public key,
fingerprint or GitHub login by a client". No test proves a negative, so the
criterion is discharged by testing what is checkable:

- the reply carries no `public_key`, `fingerprint` or `login` field;
- a handle equals none of `crypto.fingerprint(public_key)`, the raw key, or
  the login;
- **two registries with different secrets produce different handles for the
  same public key.**

The last is the load-bearing one. It is what demonstrates an HMAC rather
than a bare digest a token-holding client could recompute for itself —
which is the actual risk, and is testable.

### Scope

Server-side only. `mycelium-client` reads `message["text"]` and tolerates
the new fields with no change; `Flow` consumes them in #59, which is where
a per-flow report makes sense — a one-shot CLI request has exactly one hop,
so grouping it by node tells the user nothing.

`CONTEXT.md` gains **handle**. `docs/OPERATIONS.md` is untouched: the
user-facing explanation of exposure belongs to #57 and #59, and documenting
wire fields nobody can consume yet would describe plumbing rather than a
capability.

## Sequencing

Built on `main` after #55 merged, because #55, #63 and #58 all rewrite the
same block of `_handle_complete_request` and reconciling two independent
rewrites of the request path is a conflict nobody should have to resolve.
#58 follows this one for the same reason: it adds `ClientRequestError` to a
retry loop whose shape this slice settles.

## Out of scope

Persisting the handle secret. Any client-side consumption. Handles in the
operator status view, which already shows real fingerprints and logins to
the operator who is entitled to them. Per-model reputation. Everything the
Phase 2 implementation design already excluded.
