# Issue #7 — Node Agent Wraps and Manages the Local vLLM Process — Design

Date: 2026-08-14
Status: Approved, not yet implemented
Issue: [#7 — Node agent wraps and manages the local vLLM process](https://github.com/Zenkai-Dynamics/Mycelium/issues/7)

This is the condensed record of the decisions made while brainstorming/
grilling issue #7, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established
in the [issue #1](2026-08-12-issue-1-project-skeleton-design.md),
[issue #2](2026-08-14-issue-2-dependency-pinning-design.md),
[issue #3](2026-08-14-issue-3-developer-setup-guide-design.md),
[issue #4](2026-08-14-issue-4-node-network-reachability-design.md),
[issue #5](2026-08-14-issue-5-coordinator-node-transport-design.md), and
[issue #6](2026-08-14-issue-6-validate-model-vllm-design.md) design docs.

## What issue #7 asks for

The node agent (stubbed in #1, currently only dials out and holds a
connection to the coordinator per #5) takes over starting, stopping, and
monitoring the vLLM server validated directly in #6, and can forward a
request to it locally — so the node agent, not a manually-run `vllm
serve` command, is what serves inference from here on. Acceptance
criteria: starting the node agent automatically starts a local vLLM
server hosting the Phase 0 model; stopping the node agent cleanly stops
the vLLM process with no orphaned GPU processes; a prompt sent to the
node agent (not directly to vLLM) is forwarded to vLLM and returns a
correct completion.

Coordinator-side registration, heartbeat, and request routing
(#8, #9, #10) do not exist yet — #7 is scoped to what the node agent can
prove standalone, independent of that plumbing.

## Decisions made

**Serving mechanism: subprocess running `vllm serve`, not
`ray.serve.llm` or an in-process engine API.** `ray[llm]==2.57.0` is
already pinned in `pyproject.toml`, but per
[issue #6](2026-08-14-issue-6-validate-model-vllm-design.md)'s design,
that pin exists only because `ray[llm]` hard-pins the matching `vllm`
version internally — it isn't a commitment to using Ray's serving layer.
Considered and rejected `ray.serve.llm`: its actual value is orchestrating
multiple model replicas across a *Ray cluster*, which Mycelium's
coordinator/node-agent split already does its own way across
independently-owned, non-clustered machines (see ADR-0002) — layering
Ray's own cluster-routing on top would duplicate the coordinator's job
(#10), not help it. Multi-GPU tensor parallelism, if a future model ever
needs it, is available from plain `vllm serve --tensor-parallel-size N`
without Ray at all. Also rejected an in-process vLLM Python API
(`AsyncLLMEngine`): it would diverge from the exact `vllm serve` HTTP
server #6 already validated on real hardware, and loses subprocess-level
crash isolation for no benefit at Phase 0's scale. `subprocess.Popen`
running the literal `vllm serve <model> --port <port>` command reuses
#6's real-hardware evidence directly.

**Forwarding surface: an internal async method plus a one-shot `--prompt`
CLI flag — no new network-exposed endpoint on the node agent.** Since
#8-#10 (registration, heartbeat, routing) aren't built yet, "a prompt
sent to the node agent" can't mean "routed there by the coordinator."
Considered exposing a small standalone HTTP server on the node agent
(e.g. `POST /complete`) purely so a prompt could be sent to it over the
network right now — rejected as throwaway surface #10 would likely
replace once real coordinator routing lands, and it's speculative
complexity Phase 0 doesn't need. Instead, `vllm_process.complete(prompt,
port)` is the single forwarding entry point: unit-tested directly, and
exercised for real (GPU-hardware) verification via
`mycelium-node --prompt "..."`, a one-shot mode that starts vLLM, sends
that one prompt through the same method, prints the result, stops vLLM,
and exits. #8-#10 will later call this same method from inside the
websocket message handler rather than any HTTP surface being replaced.

**Configuration: `--model`, `--gpu`, and `--vllm-port` are CLI flags,
each with a Phase 0-appropriate default.** `--model` defaults to
`Qwen/Qwen2.5-7B-Instruct` (Phase 0's one chosen model, per
`docs/phases/phase-0-foundation.md`) but is overridable rather than
hardcoded, since nothing about the node agent's code should assume
exactly one model forever. `--gpu` defaults to `"0"` and becomes
`CUDA_VISIBLE_DEVICES`; `--vllm-port` defaults to `8811`, matching the
port #6 already verified against. These are made flags rather than
constants because #6's own real-hardware run already needed
node-specific values (`a6000`'s single-GPU pin) that a different future
node would need to override.

**`HF_HOME` is not managed by the node agent.** The vLLM subprocess
inherits the parent process's environment unchanged (aside from the
explicit overrides below) — an operator sets `HF_HOME` the same way they
did manually for #6's verification, before launching `mycelium-node`.
Rejected adding a `--hf-home` flag: it's a generic Hugging Face cache
concern, not something specific to how Mycelium wraps vLLM, and #6
already showed the right value is entirely a per-machine storage
decision (`a6000`'s tight root disk drove its own cache path) that an
operator, not a CLI default, is best placed to make.

**`VLLM_USE_FLASHINFER_SAMPLER=0` is always injected, unconditionally —
not a flag.** Carries forward the known limitation #6 documented: a
`flashinfer`/CCCL packaging inconsistency breaks its JIT-compiled
sampling kernel, and vLLM's native sampler fallback is what actually
works. `docs/phases/phase-0-foundation.md`'s open-risks section already
calls out that #7 needs to set this — it's a correctness requirement,
not something an operator should need to remember to pass.

**Coordinator arguments become optional; either they or `--prompt` (or
both) must be supplied.** Today `--coordinator-url`/`--coordinator-cert`
are required (enforced by `parse_args`, asserted in
`tests/node/test_cli.py`). Considered keeping them required always and
letting `--prompt` run alongside a real (or dummy) coordinator
connection — rejected as awkward: the one-shot mode's entire point is
verifying #7 in isolation, the same way #6 verified vLLM in isolation
with no coordinator anywhere in the picture. Post-parse validation now
requires at least one of `{--prompt, coordinator args}`; the two
existing "required" tests in `tests/node/test_cli.py` change accordingly
to reflect the new contract.

**Process lifecycle: process-group kill, not a bare PID kill.**
`subprocess.Popen(..., start_new_session=True)` puts `vllm serve` in its
own process group; `vllm_process.stop()` sends `SIGTERM` to the whole
group via `os.killpg`, waits with a bounded timeout, and escalates to
`SIGKILL` if it hasn't exited. A bare PID kill would risk orphaning
`vllm serve`'s own multiprocessing worker subprocesses (the engine-core
process vLLM spawns internally) — exactly the "orphaned GPU processes"
the acceptance criteria calls out.

**Readiness: poll vLLM's `GET /health` until `200`, bounded timeout,
fail fast.** Matches the fail-fast principle
`docs/phases/phase-0-foundation.md` already establishes for the
coordinator ("if no healthy node ... fails the request immediately with
a clear error"). A node agent that hung indefinitely waiting for a vLLM
server that will never come up would be a worse failure mode than a
clear, bounded timeout error.

**HTTP client for the forwarding call: stdlib `urllib.request` inside
`asyncio.to_thread`, not a new dependency.** The node agent currently
depends on nothing beyond `websockets` and `cryptography`, both
load-bearing for the transport itself. Considered `httpx` and `aiohttp`
for cleaner async ergonomics — rejected for now: this is one local HTTP
call per prompt against `localhost`, not a throughput-sensitive path,
and this project has treated every dependency addition as deliberate
(see [issue #2](2026-08-14-issue-2-dependency-pinning-design.md)'s
dedicated pinning process) rather than default-adding a library for
convenience. `asyncio.to_thread` keeps the blocking stdlib call from
stalling the event loop that also runs the coordinator connection.

**Module layout: new `mycelium/node/vllm_process.py`.** Houses the
subprocess lifecycle (`build_command`, `build_env`, `start`,
`wait_ready`, `stop`) and the forwarding call (`complete`) together,
since they're tightly coupled — `complete` talks to the port `start`
just launched on. `mycelium/node/cli.py` gains the new flags and
orchestrates: start vLLM → wait ready → either one-shot `--prompt`
handling or the existing coordinator connect loop → `finally: stop
vLLM` on any exit path, including `SIGINT`/`SIGTERM`.

**Testing: a fake local HTTP server stands in for vLLM in automated
tests; real hardware confirms the rest.** Mirrors
`tests/node/test_connection.py`'s existing pattern of a real local
server rather than mocks. The fake server implements just enough of
`GET /health` and `POST /v1/chat/completions` to exercise
`wait_ready`/`complete` and the start/stop subprocess lifecycle without
needing a GPU or vLLM installed in CI. A real-hardware live-verification
narrative — run via `mycelium-node --prompt "..."` against the real
`a6000` node (`training-framework@192.168.22.23`, already in the
operator's SSH config, the same machine #6 verified against) — confirms
an actual correct completion and confirms no leftover GPU process after
exit (`nvidia-smi` before/after), following #6's precedent of not
trusting real-hardware behavior to be identical to a fake stand-in.

## Live verification

Run for real against the `a6000` node
(`training-framework@192.168.22.23`), after the code above was
implemented, reviewed (including a final whole-branch review that found
and fixed a real Critical bug — see below), and tested locally. The repo
checkout (this branch) was synced to `/mnt/disk1/Framework/mycelium-repo`
via `rsync` (the repo is private, so a plain `git clone` from the node
failed on missing credentials — copying the working tree directly sidesteps
that, matching #6's own precedent of copying files rather than cloning),
and `uv sync --extra node` installed the pinned stack cleanly, reusing the
HF cache already populated at `/mnt/disk1/Framework/mycelium-hf-cache`
from #6 (no re-download needed).

**GPU choice: `--gpu 2`, not `--gpu 0`.** `nvidia-smi` at verification time
showed GPUs 0, 1, and 3 already carrying other users' workloads (16.8 GB,
16.8 GB, and 29.4 GB used respectively — this is a shared machine), while
GPU 2 was fully idle (4 MiB). This is exactly the scenario the `--gpu` CLI
flag exists for: #6 could hardcode `CUDA_VISIBLE_DEVICES=0` because it
only had to prove single-GPU fit once; a long-lived node agent needs to
target whichever card is actually free, which varies run to run.

**One-shot `--prompt` run:**

```
$ HF_HOME=/mnt/disk1/Framework/mycelium-hf-cache mycelium-node \
    --prompt "What is the capital of France? Answer in one word." \
    --gpu 2 --vllm-port 8812
starting vLLM (Qwen/Qwen2.5-7B-Instruct on GPU 2)...
[...vLLM startup log, model load, CUDA graph capture...]
vLLM ready
Paris
```

The node agent — not a manually-run `vllm serve` command — started vLLM,
waited for it to become healthy, forwarded the prompt, and printed the
correct completion (`"Paris"`, the same prompt/answer #6 verified directly
against raw vLLM, for continuity). After the process exited, `nvidia-smi`
showed GPU 2 back at 4 MiB — the full ~14.3 GiB weight + KV-cache
allocation was released, and `ps aux` showed no leftover `vllm`/
`mycelium-node` process. This closes acceptance criteria 1 and 3.

**SIGTERM check, long-running mode — closing acceptance criterion 2 on
real hardware, not just against the test suite's fake process.** The final
whole-branch review (see the implementation plan's ledger) found a real
Critical bug here before this: no signal handler existed, so `SIGTERM`/
`SIGHUP` to the node agent bypassed the `finally: stop vLLM` cleanup
entirely, orphaning vLLM. That was fixed (`main()` now installs a
synchronous handler for `SIGTERM`/`SIGHUP`/`SIGINT` that calls
`process.stop()` directly) and covered by a new automated regression test
before this live check ran. Re-verified for real: started
`mycelium-node` in normal (long-running, no `--prompt`) mode against a
deliberately unreachable coordinator address, confirmed `vllm serve` had
spawned two child processes under its own process group exactly as
expected —

```
$ ps -o pid,pgid,cmd -p <vllm-pid>
71565  71565  vllm serve Qwen/Qwen2.5-7B-Instruct --host 127.0.0.1 --port 8813
$ ps --ppid 71565 -o pid,ppid,cmd
72005  71565  python -c from multiprocessing.resource_tracker import main;main(54)
72006  71565  VLLM::EngineCore
```

— sent `SIGTERM` to the node-agent process from outside, and confirmed
all three (the vLLM leader and both children) were gone within seconds,
with GPU 2 back to its 4 MiB baseline. This is the real shape the process
group kill has to handle — vLLM genuinely spawns more than one child
process — not just the single-child shape the local test fixture
simulates.

## Explicitly out of scope for this issue

Node registration, auth-token handling, coordinator-side node registry
(#8). Heartbeat/liveness tracking (#9). Actually routing a client request
from the coordinator to this node (#10) — `complete()` exists as a
method #10 can call later, but nothing wires the websocket connection to
it in this issue. `ray.serve.llm` or any Ray-orchestrated serving.
Tensor-parallel / multi-GPU model splitting (no Phase 0 model needs it;
#6 confirmed `Qwen/Qwen2.5-7B-Instruct` fits a single GPU with room to
spare). Any new network-exposed endpoint on the node agent beyond the
existing coordinator connection. Managing `HF_HOME` or any other generic
Hugging Face cache configuration.
