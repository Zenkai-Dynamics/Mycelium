# Issue #61 — Live Verification and Latency Measurement — Design

Date: 2026-09-06
Status: Approved, not yet executed
Issue: [#61 — Live verification and latency measurement on real hardware](https://github.com/Zenkai-Dynamics/Mycelium/issues/61)
Blocked by: [#60 — Reference agent](https://github.com/Zenkai-Dynamics/Mycelium/issues/60) (merged)
Precedent: [issue #49's design doc](2026-08-29-issue-49-live-verification-design.md), whose
own precedent was issue #25's results appended to [issue #11's doc](2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md)

The condensed record of decisions made while brainstorming and grilling #61,
before running it. Like #49, this ticket contains no code — it is a
live-execution runbook, not an architecture — so no implementation plan
follows it.

**Unlike #49, the results do not land in this document.** #61's own text
directs them to a `## Live verification` section of
[the Phase 2 design doc](2026-09-04-phase-2-multi-llm-agentic-design.md), and
that is where they belong: the phase doc is where the open question about
multi-hop latency was originally posed, so the answer should sit beside the
question rather than in a ticket file a later reader has no reason to open.

## What #61 asks for

Phase 2 is merged and unit-tested against fakes: `messages` end to end (#55),
exposure handles and per-hop timing (#63), client-caused fault
classification (#58), the `Flow` library (#59) and a reference agent (#60).
None of it has run against real models on real GPUs.

#61 proves it does, and answers with data the question this phase has carried
since its first brainstorm: **is multi-hop latency actually usable?**
[ADR-0003](../../adr/0003-client-side-orchestration.md) chose client-side
orchestration knowing it costs (N−1) extra client↔coordinator round trips,
expected that to be noise against multi-second inference, and committed to
measuring rather than assuming.

## Preconditions observed before designing

Gathered from the box rather than assumed:

- `a6000` is `192.168.22.23`, reachable from the operator's Mac over SSH
  **and on arbitrary TCP ports** — verified with a throwaway listener, then
  confirmed closed again.
- **45 ms average RTT** Mac→box (`ping`, 5 packets, 0% loss). High for a
  192.168 address, so the path is routed rather than a flat LAN — which makes
  it a realistic stand-in for the deployment ADR-0003 reasoned about.
- GPUs **0, 1, 2 idle** (49 GB each, 0% utilisation). **GPU 3 reports a
  hardware fault**: `Unable to determine the device handle for GPU3:
  0000:A1:00.0: Unknown Error`.
- The box's `~/Mycelium` checkout is at `e30b77f` — **five PRs behind**,
  predating all of Phase 2.
- `Qwen/Qwen2.5-7B-Instruct` is cached; the 1.5B is not. Disk is **89% full**
  with 103 GB free.
- `~/.mycelium/github-token` is present from #49's session, so node
  registration needs no device-flow sign-in.

## Decisions

### The client runs on the Mac, not on the box

This is the decision the whole session turns on, and it is where #61 departs
from #49.

#49 put everything on `a6000` loopback because its subject — identity, caps,
reputation, bans — was orthogonal to network topology. #61's subject **is**
topology: the specific claim under test is that (N−1) extra
*client↔coordinator* round trips are affordable. Running the client on
loopback would measure that overhead as approximately zero and let us
conclude "client-side orchestration is free" on evidence that guaranteed the
answer before the first flow ran.

So the coordinator and both nodes run on `a6000`, and the client and
measurement harness run from the Mac across the real 45 ms path.

### Two models, two nodes, one GPU each

`Qwen2.5-1.5B-Instruct` on GPU 0, `Qwen2.5-7B-Instruct` on GPU 1. GPU 2 is
left free for other users; GPU 3 is untouched.

One model per node is not incidental — it is what makes hop→node attribution
unambiguous for the exposure check below. Co-locating both models on one GPU
was rejected: the two nodes would contend for the same compute, so per-hop
latency would partly measure that contention rather than the thing under
test.

`nvidia-smi` is re-checked before each phase, not once at the start. #49
found GPU availability changing more than once mid-session; the standing rule
is to adapt or stop rather than degrade another user's job.

### Latency comes from a throwaway harness, not from the reference agent

The reference agent prints what each hop *sent* — the explicit-context rule
made visible — but never `wall_ms` or `elapsed_ms`. Running it produces no
numbers.

So the agent is run **once, unmodified**, satisfying its own acceptance
criterion that it completes a real two-model flow. The measurement is a
separate short harness on the Mac that drives `Flow` directly, runs the
two-hop flow repeatedly, and reports per-hop timings.

The harness is **not committed to the package**. #61 is a verification
ticket, and #49's precedent is that these sessions ship no code. Its source
goes verbatim into the results section, so the measurement stays
reproducible without adding a feature nobody asked for.

*(Noted in passing, not fixed here: that the reference agent surfaces
exposure but silently drops the timing data a `Hop` already carries is
arguably a gap in #60. It is not this ticket's to close.)*

### One warm-up flow discarded, then ten measured

A single flow is an anecdote, and the first one additionally pays vLLM's
cold-start and cache warming — which would inflate precisely the number under
test. Ten flows give a median and a visible spread per hop.

Thirty-plus was rejected as costing shared GPU time for precision the
conclusion does not need: at 45 ms RTT against multi-second inference, ten
samples already settle whether the overhead is noise or significant.

### Exposure is cross-checked against the coordinator, not self-checked

With one model per node, hop 1 must have been served by node A and hop 2 by
node B. So the client's two handles must be **distinct**, and **stable across
all ten flows**. Then `mycelium-coordinator-status` gives the coordinator's
own per-node completion counts, which must equal the number of flows each
node served.

That is an independent view — the coordinator's — against what the client's
report claims, which is what "accurate against what the nodes actually
received" asks for. Reading vLLM's request logs would be stronger still, but
the completion counters answer the question with no extra instrumentation.

### Session isolation

A fresh `~/.mycelium61/` holds a newly generated shared token and TLS cert,
so nothing this session does touches the operator's normal setup or #49's
leftover `~/.mycelium49/`.

The one deliberate exception is `~/.mycelium/github-token`, reused as #49
did: it is already cached, it avoids a browser sign-in mid-session, and it is
what a real volunteer's setup would use. Re-proving the device flow is #49's
job, not this one's.

The coordinator binds `0.0.0.0` on a high port for the session — required for
the remote client to reach it at all. It is TLS with a pinned self-signed
cert and gated by a session-only token, so the port is reachable on the lab
network but not usable. An SSH tunnel was rejected: it would put SSH's own
encryption and buffering inside the latency path this session exists to
measure.

### Teardown

vLLM processes and the coordinator stopped and **confirmed** gone, the port
confirmed closed, `~/.mycelium61/` removed, GPUs back to idle.

The checkout is left updated at `main` rather than rolled back — an
out-of-date checkout was itself a finding in #49, and current is the useful
state. The prior commit (`e30b77f`) is recorded so it is reversible. The
downloaded 1.5B weights stay in the HF cache; this is the model pair the
project intends to keep using.

## A criterion that cannot be fully met

#61's first acceptance criterion asks that both models be **"discoverable by
a client"**. Client-side discovery does not exist: `list_models` is #56, which
is unbuilt, and #59 deliberately omitted `Flow.list_models()` for exactly that
reason.

The session verifies both models are genuinely *served* by two real nodes via
`mycelium-coordinator-status`, and records the criterion as **partially met**,
pointing at #56. Treating the operator status view as client discovery was
rejected: that view deliberately exposes the fingerprints and GitHub logins
#56 exists to withhold from clients, so calling it client discovery would
misrepresent what was verified — in a results section later readers will
trust.

## What gets recorded

Appended to the Phase 2 design doc as `## Live verification`, with real
command transcripts:

1. Setup, including the environment drift found and any GPU contention.
2. The reference agent's single unmodified run.
3. Per-hop and end-to-end latency: median and range over ten flows, with
   client↔coordinator overhead (`wall_ms − elapsed_ms`) reported separately
   from inference time.
4. The exposure cross-check.
5. **A stated conclusion on whether multi-hop latency is usable**, resting on
   the numbers — reported honestly if they contradict the expectation. ADR-0003
   noted the wire protocol is identical either way, so a coordinator-side
   orchestrator remains an additive change if the data demands it.
6. Environment findings worth carrying forward: the GPU 3 fault, the 89% disk,
   and the checkout drift.

A real bug found during the session becomes its own follow-up ticket rather
than being silently patched here.
