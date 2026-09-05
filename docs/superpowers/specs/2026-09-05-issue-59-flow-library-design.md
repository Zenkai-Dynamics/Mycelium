# Issue #59 — Multi-Hop Client Library — Design

Date: 2026-09-05
Status: Approved, not yet implemented
Related: [Phase 2 implementation design](2026-09-04-phase-2-implementation-design.md),
[ADR-0004 — explicit per-hop context](../../adr/0004-explicit-per-hop-context.md),
[issue #63's design](2026-09-05-issue-63-exposure-handles-design.md) (exposure handles),
[issue #58's design](2026-09-05-issue-58-fault-classification-design.md) (the `fault` field),
[issue #59](https://github.com/Zenkai-Dynamics/Mycelium/issues/59),
[PRD: issue #54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54)

The primitive agents are written against: a `Flow` that carries one logical
task across several models, keeping a complete local record of what happened
while sending only what each call explicitly names.

This is the slice-level record of decisions taken with the code in front of
us. The cross-slice Phase 2 design settled the API's shape; three things have
changed since it was written, and this document resolves them.

## What changed since the cross-slice design

**`Flow.list_models()` has nothing to call.** The earlier design put discovery
on Flow's surface, but #56 has not been built, so no `list_models` request
exists on the wire. It is dropped from this slice. #59's acceptance criteria
never mention discovery; it was a convenience from the API discussion. Adding
the method when #56 lands is purely additive, and #60's reference agent takes
its model names as arguments, so nothing downstream is blocked.

**The `fault` field now exists.** #58 shipped it after the Flow API was
sketched. `HopError` carries it, which is what it was added for.

**A hop's exposure is a list, and an identity handle can be null.** #63's
failover handling made `exposed` a list per hop, and an unbound identity
yields a null handle. The exposure report has to represent both.

## Decisions

### The record is evidence, so `call` deep-copies what it sends

An agent that reuses and mutates a message list across hops is a natural thing
to write. If `Hop.sent` held a reference, doing so would retroactively rewrite
the record of what was already sent — and that record is the evidence behind
the exposure report, the thing that makes ADR-0004's claim checkable rather
than trusted. Evidence that ordinary Python aliasing can edit after the fact
is not evidence.

A shallow copy was rejected: it protects against appends and reorderings but
not against editing a message's `content` in place, which is exactly what an
agent trimming its context would do. Documenting "don't mutate" was rejected
on the same grounds ADR-0004 rejected opt-in privacy — a property that depends
on every author reading and obeying a docstring is not a property.

### `HopError` carries the fault and the hop

```python
try:
    review = await flow.call("big-model", messages=[...])
except HopError as exc:
    if exc.fault == "client":
        ...  # trim context and retry
    else:
        ...  # a different model, or give up
    exc.hop.exposed  # who saw it anyway
```

An agent seeing `"client"` should fix its request; one seeing `"node"` may
retry the same content elsewhere. Exposing it as an attribute rather than
leaving it in prose avoids the string-sniffing across a component boundary
this project already rejected at the coordinator. The error also carries the
recorded `Hop`, so a handler has the exposure and timing without reaching back
into `flow.hops`.

### Transport does a dumb round-trip; callers own their error vocabulary

```python
async def request(coordinator_url, coordinator_cert, message: dict, timeout: float) -> dict
```

Connect, send, await one reply, close, return it parsed. It knows nothing
about completions, faults or exposure. Transport-level failures raise
`TransportError`; a `complete_error` in the reply is data the caller
interprets.

Three things count as transport-level: a timeout, the coordinator closing
without replying, and the coordinator being unreachable. The third is worth
naming because it arrives differently — a refused or unroutable connection
raises `OSError` out of `websockets`, not `ConnectionClosed` — and if it
escaped unwrapped, `Flow` could not record the attempted hop, leaving a gap
in a record whose completeness is the point.

The alternative, having transport raise on `complete_error`, would couple it
to one caller's error type, which the other would then catch and re-raise —
translating an exception it never wanted rather than reading a dict.

**`cli.complete()` migrates onto it in this slice.** That migration is the
justification for extracting at all rather than letting `Flow` keep a private
round-trip: two implementations of connect-send-receive-close against a wire
that #63 and #58 both just changed is precisely the drift the extraction
prevents. The operator CLIs (`status`, `ban`) are not touched — different
concern, and this phase has no business editing them.

### No client-side validation of `messages`

#55 deliberately made the coordinator the single validation point, and #58
made a rejected request return a clear typed error that costs the volunteer
nothing. A second copy of the rule in the client is a second thing to keep in
step, and a copy that drifted stricter would reject requests the network would
have accepted. Flow requires `messages` at the API level — that is its own
signature, not a restatement of the wire's policy.

### The record shape

```python
@dataclass(frozen=True)
class Hop:
    index: int
    model: str
    sent: list[dict]          # deep copy, taken at call time
    text: str | None
    error: str | None
    fault: str | None
    exposed: list[dict]       # [{node_handle, identity_handle}, ...]
    elapsed_ms: int | None    # absent when no node was attempted
    wall_ms: int


@dataclass(frozen=True)
class Exposure:
    by_node: dict[str, list[int]]
    by_identity: dict[str | None, list[int]]
```

A failed hop is as complete a record as a successful one — #59's acceptance
criteria require the record to stay intact and usable after a failure, which
a separate failure list would not satisfy. Client↔coordinator overhead is
derivable per hop as `wall_ms − elapsed_ms`, which is what #61 measures.

`exposure()` returns both groupings rather than a flat list of pairs: the
reference agent and the live-verification ticket both want exactly these two
views, and a flat list would have every consumer writing the same grouping
loop.

**A null identity handle is kept under a `None` key, never dropped.** A node
whose bound identity could not be resolved still saw the hop; omitting it
would under-report exposure, the failure direction #63's fix wave existed to
close.

### Hops are appended on completion, and `hops` is ordered by index

A hop's index is assigned when the call *starts*, so `asyncio.gather` across
several models produces a record in the agent's call order rather than in
whichever order the models answered. The Hop itself enters the record once it
has succeeded or failed, so `flow.hops` never contains a half-populated entry
that every consumer — including the exposure report — would have to check
before trusting. Mid-flight, `flow.hops` shows the hops that have finished,
which is an honest answer to "what has happened so far".

### The import surface

```python
from mycelium.client import Flow, Hop, HopError, Exposure, system, user, assistant
```

`mycelium/client/__init__.py` re-exports, so `flow.py` and `transport.py` stay
free to change shape — which matters because `transport` is new and may well
move. Leaving the package empty would make the submodule paths de-facto
public in a package whose stated design is a small primitive surface.

## Testing

The load-bearing test is the explicit-context one, and it must check the
**wire**, not the library's internal state: capture the raw frames a fake
coordinator received and assert the exact JSON of each hop. A test that
inspected `Hop.sent` would pass even if `call` sent something else.

Then: mutating the caller's list after a call leaves `hop.sent` unchanged; a
failed hop is in the record *before* `HopError` surfaces, carrying its fault;
`exposure()` groups by node and by identity including the `None` key; a
`gather` of three calls records them in call order; and the entire existing
client CLI suite stays green through the transport migration, which is the
evidence that migration changed no behavior.

## Scope

`docs/OPERATIONS.md` gains a short task-oriented section — writing an agent
that uses several models — stating the explicit-context rule plainly and
showing the exposure report being read. Not an API reference; that guide is
task-oriented and this follows it.

Out: `list_models()` (returns when #56 lands), the reference agent (#60), live
verification (#61), any coordinator or node change, and the operator CLIs.
