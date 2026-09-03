# Issue #49 — Live-Hardware + Real-GitHub Verification of Phase 1 — Design

Date: 2026-08-29
Status: Approved, not yet executed
Issue: [#49 — Live-hardware + real-GitHub verification of Phase 1's open-network flow](https://github.com/Zenkai-Dynamics/Mycelium/issues/49)
Blocked by: [#48 — Register Mycelium's real GitHub App](https://github.com/Zenkai-Dynamics/Mycelium/issues/48) (closed)
Precedent: [issue #11's design doc, "Live verification" section](2026-08-16-issue-11-coordinator-reroutes-when-node-goes-down-design.md)
(issue #25 — the pattern this doc's own eventual results section follows)

This is the condensed record of the decisions made while brainstorming/
grilling issue #49, before running it. Like #38/#48, this ticket has no
code in it — it's a live-execution runbook, not an architecture — so no
separate implementation-plan doc follows this one. Once the session
actually runs, its results get appended to *this* doc as a
`## Live verification` section, matching how issue #25's results were
appended to issue #11's own design doc rather than scattered across
every ticket the session touches.

## What issue #49 asks for

Everything Phase 1 shipped (#33 keypair identity, #39 GitHub verification,
#35 per-identity cap, #36 reputation-weighted selection, #37 manual ban,
#34 device-flow sign-in) is merged and unit/integration-tested against
injected fakes, but none of it has run against a real GitHub identity or
real GPU hardware. #49 proves four things end to end, on the operator's
`a6000` box, using the real GitHub App #48 registered: (1) a real node
completes the device-flow sign-in and registers; (2) the per-identity cap
rejects a real registration once the identity is at its limit; (3)
reputation-weighted selection statistically favors a real reliable node
over a real unreliable one, without a hard cutoff; (4)
`mycelium-coordinator-ban` disconnects a real identity's node(s) and
blocks its future registration.

## Decisions made

**Topology: everything on `a6000`, loopback — not #25's fuller 3-host
separation.** #25 already proved the underlying transport works across
genuinely separate machines (client, coordinator, and nodes on three
different hosts); re-proving that here would retread ground rather than
cover new ground, since #49's actual subject is Phase 1's *trust* layer
(identity, cap, reputation, ban), which is orthogonal to network topology.
Matches #10's original simplest pattern instead: coordinator and both
nodes on `a6000`, client run from the same box or the operator's own
machine.

**Session shape: one continuous coordinator+node session for all four
scenarios, not four separate ones.** Register once, then layer cap →
reputation → ban on top of the same real bound identity. Avoids repeating
vLLM's multi-minute weight-load boot up to four times, and matches how
#25 itself ran multiple scenarios back to back in one live session rather
than restarting between them.

**Two real nodes, not one — because scenario 3 needs a fair comparison.**
Scenario 1 only strictly needs one real node, but scenario 3 (reputation-
weighted selection) requires comparing *selection frequency* between a
reliable and an unreliable node hosting the same model — that comparison
is only meaningful if both are real, running nodes capable of actually
serving the model, not one real node and something synthetic. So Node A
and Node B both run real `vllm serve` instances from the start; Node B
starts as the one that later accumulates real failures.

**Per-identity cap (scenario 2): one lightweight stand-in fills the cap,
a second lightweight stand-in gets rejected — not four full real nodes.**
The per-identity cap check fires when the coordinator receives a signed
`register` message; it has no dependency on whether the sending node's
vLLM is healthy or even exists. Since Node A and Node B (both needed
anyway for scenario 3) already occupy two of the cap's three default
slots under the same real bound identity, only one additional
registration is needed to fill the cap, and one more after that to
observe the rejection — both as lightweight scripts that reuse
`mycelium.crypto`/`mycelium.node.registration`/`mycelium.node.identity`
directly to send a real, validly-signed registration over a real
websocket connection to the real coordinator, without ever starting
vLLM. This is still a fully real exercise of the coordinator's identity-
resolution, cap-enforcement, and network code — a connection that only
ever existed to prove a rejection was never going to serve real traffic
regardless of whether vLLM sat behind it, so skipping that GPU-heavy step
changes nothing about what's actually being verified. Running four full
`mycelium-node` processes (needing up to four simultaneously-idle GPUs)
was considered and rejected as unnecessary GPU time and setup for
identical proof value.

**Correction found during grilling, verified against the actual code:**
`enforce_identity_cap`'s own docstring is explicit — it "counts live
registry occupancy... a disconnected node's old key doesn't count against
the cap," and per #35's own design a node disconnecting frees its slot
*immediately*. A stand-in that registers and then exits would **not**
hold its cap slot. The stand-in script must keep its websocket connection
open (idle — no further traffic needed) for as long as it needs to keep
occupying a slot: from filling the cap in scenario 2, through the second
stand-in's rejection, **and through scenario 4's ban** (see below) — only
closing once the whole session's identity-scoped work is done.

**Reputation (scenario 3): real crash and disconnect signals, not a
forced timeout.** Verified against the actual code (`router.py`,
`request_handler.py`, `vllm_process.py`), not assumed:

- *Crash* (`NodeError`/`record_crash`): killing only Node B's `vllm
  serve` subprocess — leaving its `mycelium-node` process and coordinator
  websocket connection alive — makes the next completion request fail
  fast and genuinely: `urllib`'s call to the now-dead local vLLM port
  gets connection-refused almost immediately, `request_handler.py`'s
  broad `except Exception` turns that into a real `complete_error`, and
  the coordinator's `router.route_request` raises `NodeError`. Fast,
  repeatable, no artificial code paths involved. Concrete, precedented
  command (matches `docs/OPERATIONS.md`'s own troubleshooting section —
  not a new technique): `pgrep -f "vllm serve.*--port <B's port>"` to
  find the PID, `ps -o pgid= -p <pid>` to confirm its process-group id,
  then `kill -9 -<pgid>` (the leading `-` targets the whole group,
  leaving Node B's own `mycelium-node` process, a separate process group,
  untouched).
- *Disconnect* (`NodeDisconnectedError`/`record_disconnect`): `kill -9`
  on Node B's whole `mycelium-node` process, exactly as #25 already
  proved live for Phase 0's failover. Node B then reconnects (cached
  token, same identity) so it's eligible for selection again with its
  accumulated bad counters intact — counters live in the coordinator's
  in-memory registry, keyed by public key, and persist across a node's
  own reconnects for as long as the coordinator itself keeps running.
- **Concrete numbers, decided in advance rather than eyeballed live.**
  Reputation weight is `(completions + 1) / (total + 1)`, Laplace-smoothed
  (confirmed from `registry.py`, not assumed). Inject exactly 3 crashes +
  1 disconnect on Node B (4 total pre-batch failures, weight ≈ 0.2) while
  Node A stays clean (weight = 1.0) — an initial ≈5:1 selection-weight
  ratio. A batch of 20 client requests follows: expected result is Node A
  picked meaningfully more than half the time (the initial math suggests
  roughly 80%+, trending higher as any further Node B picks during the
  batch itself push its weight down further) **and** Node B picked at
  least once, proving the "never a hard cutoff" property. Having this
  computed in advance gives the operator something concrete to check the
  actual result against, not just an impression of "A seems to win more."
- *Timeout* (`NodeTimeoutError`/`record_timeout`) is a **documented,
  accepted gap for this pass**, matching #25's own precedent of accepting
  the h100 permission issue as a real, separate gap rather than forcing
  an artificial reproduction. The code is deliberately built so this
  case almost never fires organically: `vllm_process.COMPLETE_TIMEOUT_SECONDS`
  (120s) is set 10s *under* `router.NODE_COMPLETE_TIMEOUT_SECONDS` (130s)
  specifically so a slow node's own timeout fires first and produces a
  `complete_error` (→ `NodeError`/crash), not a coordinator-side timeout
  — confirmed via `router.py`'s own comment on why the two constants are
  10s apart. A genuine `NodeTimeoutError` needs a node to go silent on
  one specific reply while continuing to answer the websocket's ping/pong
  (otherwise the 40s heartbeat window — `PING_INTERVAL_SECONDS` +
  `PING_TIMEOUT_SECONDS`, both 20s — trips first and produces a disconnect
  instead). Attempting to force this (e.g. `SIGSTOP` timed precisely
  between the heartbeat and completion windows) was considered and
  rejected: finicky, likely to just produce another disconnect, and for a
  case the design already treats as a narrow edge rather than a normal
  failure mode.

**Recorded-results scope: incidental `mycelium-coordinator-status`
coverage, not a separate effort.** Issues #33 and #36 added identity-
fingerprint and reputation-counter fields to `mycelium-coordinator-status`'s
output, but neither ticket's own live-hardware verification ever ran (no
live-verification ticket existed for either at the time). Since this
session already runs `mycelium-coordinator-status` repeatedly to observe
cap/reputation/ban results, capturing that its output correctly displays
those fields under real conditions costs nothing beyond noting it in the
transcript — closing a related, real gap opportunistically rather than
either ignoring it or scoping a whole separate effort around it.

## Session runbook

1. **Prereqs.** Confirm SSH reachability to `a6000`. Run `nvidia-smi` —
   confirm at least 2 GPUs are idle enough to not degrade another user's
   job (matching #25's own caution; if fewer than 2 are free, wait or
   pick a different pair rather than proceeding).
2. **Setup.** Fresh token + self-signed cert generated on `a6000`;
   `mycelium-coordinator` started there.
3. **Scenario 1.** Node A starts with no `--github-token-file` and no
   cached `~/.mycelium/github-token` — completes the real device-flow
   sign-in (code printed, authorized in a browser), registers. Confirmed
   via `mycelium-coordinator-status`.
4. **Node B** starts on a second GPU, same model, pointed at the same
   cached token path Node A just populated (same real identity — the
   legitimate multi-GPU-volunteer case the docs already describe).
   Confirmed registered.
5. **Baseline.** A handful of client requests confirm both nodes are
   genuinely reachable and round-robin-eligible before any failure is
   injected — establishes the "before" state, matching #25's own style.
6. **Scenario 2.** One lightweight stand-in registration (direct use of
   `mycelium.crypto`/`identity`/`registration`, no vLLM) under the same
   identity, using its own fresh keypair and the same cached token —
   succeeds, filling the cap at 3. **It stays connected (idle) from here
   through scenario 4** — its cap slot is only held while its websocket
   is open, per `enforce_identity_cap`'s own documented behavior. A
   second stand-in attempt is rejected; the exact `registration_rejected`
   reason is recorded.
7. **Scenario 3.** Node B's `vllm serve` subprocess is killed via
   `pgrep`/`ps -o pgid=`/`kill -9 -<pgid>` (its `mycelium-node` process
   stays up). 3 client requests routed to Node B are recorded failing
   with a real `NodeError`; `mycelium-coordinator-status` shows Node B's
   crash counter at 3. Node B's whole process is then `kill -9`'d for one
   real disconnect (counter → 1), and restarted (reconnects with its
   cached token, no re-auth) — 4 total pre-batch failures, weight ≈ 0.2
   against Node A's clean weight of 1.0. A batch of 20 client requests is
   sent; the A/B split is recorded against the ≈80%+/at-least-once
   expectation computed in the design decisions above.
8. **Scenario 4.** `mycelium-coordinator-ban --identity <login>` is run
   against the real bound identity. All three currently-connected
   entities under it — Node A, Node B, and the still-open scenario-2
   stand-in — are confirmed disconnected immediately
   (`disconnected_count: 3` in the ban response,
   `mycelium-coordinator-status` shows none registered). A further
   registration attempt from that identity (Node A restarting, or a
   fresh stand-in) is confirmed rejected with the banned-identity reason.
9. **Recording.** A `## Live verification` section, dated to when the
   session actually runs, is appended to *this* design doc — real command
   transcripts, real output, any real findings/bugs encountered along the
   way (matching #25's own style, including its precedent of recording a
   real bug found mid-session rather than silently working around it). If
   a genuine bug turns up, it becomes its own follow-up ticket per #49's
   own acceptance criteria — not silently patched as part of this
   ticket's commits, since this ticket makes no code changes.

## Explicitly out of scope for this issue

A genuine coordinator-side `NodeTimeoutError` reproduction (documented gap
above). Full 3-host network separation (already proven in #25). Any code
change — a real bug found during the session becomes its own ticket. Any
change to #33/#36's status-display code — this session only *observes and
records* that it already works, it doesn't add new coverage to it.

## Live verification (2026-08-30 to 2026-09-03, issue #49)

Run on `a6000` exactly as planned: coordinator + both nodes on that box,
loopback, one continuous session (spread across several real-world days
due to VPN connectivity to the box being repeatedly unavailable — see
below). `~/.mycelium49/` used throughout as a dedicated, isolated
directory (fresh token/cert), not the operator's normal `~/.mycelium/`,
except for the GitHub token cache itself which deliberately used the real
default path (`~/.mycelium/github-token`) since that's what a real
volunteer's setup would use.

### Setup and a real environment-drift finding

`a6000`'s existing `~/Mycelium` checkout was 17 commits behind main
(dated from before #33 even merged) — no `mycelium-coordinator-ban`
binary existed yet. `git pull` + `uv pip install -e '.[node]'` brought it
current. Not a bug, just a reminder that a long-lived node's checkout
needs updating before it can be used for anything issue #34-onward.

`nvidia-smi` before starting: GPUs 0/1 idle, GPU 3 at 100% (another
user's job) — used GPUs 0 and 1 initially. GPU availability changed
significantly more than once over the course of this session (see
below) — re-checked and adapted each time rather than assuming a stale
reading, per the design's own precondition.

### Scenario 1 — device-flow sign-in, with real operational findings

This scenario alone surfaced five distinct, real issues — none of them
bugs in Mycelium's own code, all in how a headless live session actually
has to be operated:

1. **The SSH/VPN path to `a6000` was genuinely, repeatedly unstable** —
   full outages (no ICMP response at all, not just SSH) lasting minutes
   to tens of minutes, more than once, across multiple real days. This
   directly interacts with GitHub's device-flow UX: a device code expires
   in ~15 minutes, and `mycelium-node`'s own polling loop treats *any*
   network error as fatal with no retry (by design — see issue #34's
   design doc, "any other error code → catch-all fatal exit"). On flaky
   networks this means the node process itself dies, not just the current
   code. Mitigated live with an ad-hoc bash wrapper that kept restarting
   `mycelium-node` and printing fresh codes until one was authorized
   before expiring — effective, but confirms this is a real rough edge
   for volunteers on unreliable connections, worth a note for anyone
   running this in production on flaky links.
2. **A real self-inflicted `pkill -f` footgun.** Using `pkill -9 -f
   'mycelium-node.*node-a-key'` to stop a stuck node killed the *invoking
   SSH command's own shell*, not the target process — `pkill -f` matches
   full command lines, and the pattern was a literal substring of the SSH
   command's own text. Fixed with the standard `pkill -9 -f '[n]ode-a-key'`
   bracket idiom. A real, easy-to-hit operational mistake, not a Mycelium
   bug — noted here since it cost real time to diagnose.
3. **The documented `PATH` gotcha (already known, now hit for real).**
   Running `.venv/bin/mycelium-node` without also exporting
   `.venv/bin` onto `PATH` fails with `FileNotFoundError: ... 'vllm'`,
   exactly as `docs/OPERATIONS.md` already warns. Confirms that
   documented caveat is accurate and still current.
4. **A cached GitHub token went stale after several real days** and the
   exact diagnostic hint added by issue #34's final review fired for
   real: `registration failed: invalid or expired GitHub token; ...
   this looks like a stale GitHub token — delete
   /home/training-framework/.mycelium/github-token to re-authenticate`.
   Deleting the file and restarting triggered a fresh device flow as
   designed — the hint's advice is correct and was followed successfully.
5. **`kill -9` on just `mycelium-node`'s parent process orphans `vllm
   serve`, exactly as `docs/OPERATIONS.md`'s troubleshooting section
   already warns** — hit repeatedly across this session (each time
   producing a later `OSError: [Errno 98] Address already in use` on the
   next launch attempt). Recovered each time with the documented pattern:
   find the orphan via `pgrep -f 'vllm serve.*--port <port>'`,
   confirm its pgid via `ps -o pid,pgid,cmd`, `kill -9 -<pgid>` (the
   leading `-` to target the whole process group). Confirms that
   documented recovery procedure works exactly as written.

Once past these, the actual protocol behavior worked precisely as
designed. Node A, no cached token:

```
$ mycelium-node --coordinator-url wss://127.0.0.1:8765 \
    --coordinator-cert ~/.mycelium49/coordinator-cert.pem \
    --node-id a6000-node-a --gpu 2 --vllm-port 8811 \
    --node-key-file ~/.mycelium49/node-a-key.pem
First copy your one-time code: 0D95-B419
Then visit: https://github.com/login/device and enter it
```

Authorized in a browser (a different session than the one used to verify
#48, showing this works from an arbitrary GitHub account, not just the
App's own owner) — `mycelium-node` picked it up automatically, started
vLLM, and registered:

```
starting vLLM (Qwen/Qwen2.5-7B-Instruct on GPU 2)...
vLLM ready
mycelium-node 0.1.0 connecting to wss://127.0.0.1:8765
connected to coordinator (wss://127.0.0.1:8765)
registered with coordinator as 'a6000-node-a'
```

`mycelium-coordinator-status` immediately after (also the incidental
#33/#36 display-under-real-conditions coverage the design called for):

```
$ mycelium-coordinator-status --coordinator-url wss://127.0.0.1:8765 \
    --coordinator-cert ~/.mycelium49/coordinator-cert.pem \
    --token-file ~/.mycelium49/token
a6000-node-a [1b46f412a0e1] (github:Varun-Gambhir) [ok:0 timeout:0 crash:0 disconnect:0]: Qwen/Qwen2.5-7B-Instruct
```

Real fingerprint, real bound GitHub login, fresh reputation counters —
all displaying correctly.

Node B started next, reusing the cached token (no second device flow —
confirmed the "already-bound identity" fast path works exactly as
designed):

```
a6000-node-a [1b46f412a0e1] (github:Varun-Gambhir) [ok:0 timeout:0 crash:0 disconnect:0]: Qwen/Qwen2.5-7B-Instruct
a6000-node-b [85b5a8002072] (github:Varun-Gambhir) [ok:0 timeout:0 crash:0 disconnect:0]: Qwen/Qwen2.5-7B-Instruct
```

**Baseline.** Two client requests, one per node, confirmed round-robin
before any failure injection:

```
$ mycelium-client ... --prompt "What is the capital of France? Answer in one short sentence."
The capital of France is Paris.
$ mycelium-client ... --prompt "What is the capital of Japan? Answer in one short sentence."
The capital of Japan is Tokyo.
```

Each node's own log showed exactly one `POST /v1/chat/completions` —
clean 1:1 split, matching the round-robin baseline exactly.

### Scenario 2 — per-identity cap

The lightweight stand-in script (direct use of
`mycelium.crypto`/`mycelium.node.identity`/`mycelium.node.registration`,
no vLLM) worked exactly as designed. One real finding along the way: the
stand-in's first version registered with the *real* model string,
making it an unintended routing candidate for actual client
completions despite having nothing behind it to serve them — caught
before it could interfere, fixed by registering it under a distinct
placeholder model string instead (cap enforcement itself is
model-agnostic, so this changes nothing about what's being tested).

```
$ python3 standin.py standin1-key.pem standin-1
REGISTERED: standin-1 (nrlL8CAXiaHEebO1...)
```

Status confirmed 3 slots filled under one identity:

```
a6000-node-a [1b46f412a0e1] (github:Varun-Gambhir) [ok:1 timeout:0 crash:0 disconnect:0]: Qwen/Qwen2.5-7B-Instruct
a6000-node-b [85b5a8002072] (github:Varun-Gambhir) [ok:1 timeout:0 crash:0 disconnect:0]: Qwen/Qwen2.5-7B-Instruct
standin-1 [fcd920661c67] (github:Varun-Gambhir) [ok:0 timeout:0 crash:0 disconnect:0]: standin-placeholder-model
```

A second stand-in attempt, same identity:

```
$ python3 standin.py standin2-key.pem standin-2
REJECTED: identity has reached the maximum of 3 registered nodes
```

Exact reason, exact cap default (3) — confirmed live, first try.

### Scenario 3 — reputation-weighted selection

Killing only Node B's `vllm serve` subprocess (`pgrep -f 'vllm serve.*
--port 8812'` → `ps -o pgid=` → `kill -9 -<pgid>`, Node B's own
`mycelium-node` process left alive) produced real, fast, genuine
failures on the next client requests routed there:

```
$ mycelium-client ... --prompt "Say the word apple."
error: <urlopen error [Errno 111] Connection refused>
```

3 real crashes accumulated this way, confirmed via
`mycelium-coordinator-status`'s live-incrementing `crash:` counter.

**A genuinely interesting real finding, not a bug:** getting a real
*disconnect* count on demand turned out to be much harder than expected,
and revealed something worth documenting precisely. `kill -9` on a
node's whole process closes its OS-level socket essentially
instantaneously — the coordinator's own connection handler notices the
closed connection and unregisters it (removing it from
`mycelium-coordinator-status`'s listing) far faster than any external,
independently-launched `mycelium-client` process can realistically win a
race to have already been routed there first. Multiple attempts
(sequential bursts, 15–20-way concurrent bursts, log-triggered kills,
timed kills mid-generation) all missed this window. What actually
happened: `record_disconnect` **was** firing correctly on several of
these attempts — but `mycelium-coordinator-status` only lists
*currently-registered* nodes, so a node that disconnects and isn't yet
reconnected is invisible to that command, counters and all, even though
the counters themselves (keyed by public key, independent of
`_nodes`) were accumulating correctly underneath the whole time. This
was only discovered because a routine status check *after* Node B had
reconnected showed `crash:3 disconnect:6` — six real disconnects had
already been recorded across earlier attempts, invisible until the node
was back online to display them. **Lesson for anyone verifying
reputation counters live: check status while the node in question is
registered, or the counters can be silently accumulating exactly as
designed with no visible confirmation until the next reconnect.** Not a
bug — `mycelium-coordinator-status` was never asked to show
disconnected nodes' history — but a real, non-obvious operational
subtlety worth this note for the next person who tries this.

With real accumulated history (`ok:6 crash:3 disconnect:6` on Node B,
weight ≈ 0.44; Node A clean at weight 1.0), a batch of 20 sequential
client requests was sent and the split recorded:

```
Node A: 17/20 (85%)
Node B: 3/20 (15%)
```

Clearly favored, never excluded — exactly the property being verified.
(The exact ratio differs from the plan's back-of-envelope ≈80%/at-least-once
estimate, which assumed a clean staged 4-failure state; the real number
reflects organic history accumulated live across multiple real attempts,
and still comfortably satisfies both the "meaningfully more than half"
and "at least once" criteria the design set in advance.)

### Scenario 4 — manual ban

```
$ mycelium-coordinator-ban --coordinator-url wss://127.0.0.1:8765 \
    --coordinator-cert ~/.mycelium49/coordinator-cert.pem \
    --token-file ~/.mycelium49/token --identity Varun-Gambhir
banned 'Varun-Gambhir' — disconnected 3 currently-registered node(s)
```

All three entities under the identity (Node A, Node B, and the
scenario-2 stand-in, kept deliberately connected through this scenario
per the design) — `disconnected_count: 3`, matching the design's
adjusted expectation exactly.

```
$ mycelium-coordinator-status ...
No nodes registered.
```

A fresh registration attempt from the same identity, confirmed rejected:

```
$ python3 standin.py standin3-key.pem standin-3
REJECTED: this identity has been banned by the operator
```

### Summary

All four acceptance criteria met with real evidence, on real hardware,
against the real GitHub API:

- [x] Real device-flow sign-in and registration (scenario 1)
- [x] Real per-identity cap rejection, exact reason (scenario 2)
- [x] Real reputation-weighted selection, 85%/15% split, never excluded (scenario 3)
- [x] Real ban: 3 nodes disconnected, future registration rejected (scenario 4)
- [x] `mycelium-coordinator-status` confirmed correct throughout (incidental #33/#36 coverage)
- [x] Genuine coordinator-side `NodeTimeoutError` remains an accepted, documented gap (unchanged from the design)

No code bugs found. All real findings above are operational/environmental
(a stale checkout, a `pkill -f` self-match footgun, already-documented
`PATH`/orphaned-vLLM gotchas confirmed accurate, VPN instability, and the
disconnect-counter-visibility subtlety) — none require a Mycelium code
change, so none become follow-up tickets per the design's own criterion
for what would.

This closes out #31 (Phase 1) for real — all seven original sub-issues
plus both follow-up tickets (#48, #49) are now done.
