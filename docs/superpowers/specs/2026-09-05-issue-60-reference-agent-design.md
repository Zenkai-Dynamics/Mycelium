# Issue #60 — Reference Agent — Design

Date: 2026-09-05
Status: Approved, not yet implemented
Related: [Phase 2 implementation design](2026-09-04-phase-2-implementation-design.md),
[issue #59's design](2026-09-05-issue-59-flow-library-design.md) (the library this uses),
[ADR-0004 — explicit per-hop context](../../adr/0004-explicit-per-hop-context.md),
[issue #60](https://github.com/Zenkai-Dynamics/Mycelium/issues/60),
[PRD: issue #54](https://github.com/Zenkai-Dynamics/Mycelium/issues/54)

One runnable agent using two genuinely different models in a single flow — the
tracer bullet proving the primitives compose, and that writing a real agent
under the explicit-context rule is workable rather than merely possible.

Mycelium ships primitives plus one demonstration, not an agent framework.
Roles, planning and tool-calling abstractions are LangGraph's job, not an
inference router's — so this lives in `examples/`, outside the installed
package, where it cannot become a de-facto supported API.

## Decisions

### The example is tested, despite living outside the package

This is the decision that shaped everything else. `examples/` is not installed
and not importable, and the easy call would be to leave it untested — it is a
demo, and #61 runs it on real hardware.

Against that: the example is a **consumer of the `Flow` API**, and this
project has already shipped a Critical for exactly that gap. On the #55
branch, `VLLMProcess.complete()` changed signature and a second caller outside
every task's slice was missed, breaking a documented operator command. An
untested example rots the same way — silently, until someone tries to run it,
which is most expensive precisely at #61, the live-hardware session it exists
to support.

So it gets a test, and the file is shaped to allow one.

### `run()` returns the Flow; `main()` is thin

```python
async def run(coordinator_url, coordinator_cert, token,
              draft_model, critique_model, task) -> Flow
def main(argv=None) -> int
```

A script whose logic lives inside `if __name__ == "__main__"` cannot be
tested. Returning the `Flow` lets tests assert **structurally** — which model
each hop used, what it carried, in what order — rather than pattern-matching
printed text, where "hop 2 carried the draft" is indistinguishable from "hop 2
printed something containing the draft" and reformatting output breaks tests
that were checking behavior.

`main()` still gets its own test, because surfacing what each hop sent is an
acceptance criterion in its own right, not an implementation detail.

`run` also takes an optional `flow`, and `main` constructs it. That looks like
a testing affordance and isn't: `run` **raises** on a failed hop rather than
returning, so a caller that did not already hold the `Flow` would lose the
record of everything that happened before the failure — including which
volunteers had already seen content. Keeping the record in the caller's hands
is precisely what #59 built it for, and a reference agent that lost it on the
one path where it matters most would be teaching the wrong lesson.

### The critique hop is given the task, deliberately

```python
draft  = await flow.call(draft_model,    messages=[user(task)])
review = await flow.call(critique_model, messages=[user(f"Improve this answer to: {task}"),
                                                   assistant(draft.text)])
```

ADR-0004's illustrative sample shows a critique hop *not* receiving the
original task. A critic that cannot see the task cannot judge whether the
draft answers it, so this agent passes it — and prints that reasoning beside
what the hop actually sent.

That is the honest demonstration. Explicit context does not mean "send less";
it means **decide per hop, and be able to see what you decided**. The shipped
example's own exposure report will show the second volunteer seeing the task,
which is the point rather than a flaw.

Noted for whoever picks up #57: the cross-slice design assigned ADR-0004 a
Consequences note recording exactly this, and scoped it to that issue. This
example therefore lands before the note exists; its own printed reasoning
covers the gap meanwhile.

### Models are arguments with defaults

`--draft-model` and `--critique-model`, defaulting to
`Qwen/Qwen2.5-1.5B-Instruct` and `Qwen/Qwen2.5-7B-Instruct` — the pair #61
plans to run on the a6000. Defaults make it runnable with no arguments against
the intended setup; arguments make it runnable against whatever a reader
actually serves, which the acceptance criteria require.

Discovering the pair via `list_models` would be the eventual shape, but that
request does not exist until #56, and #59 deliberately omitted
`Flow.list_models()` for the same reason. Building it here would block #60 on
#56.

### A failed hop branches on `fault`

`HopError` is caught. `client` means the request was the problem, `node` the
volunteer's, `None` that it never reached one. The example prints the reason,
the fault and its meaning, the exposure accumulated so far, then exits
non-zero.

Demonstrating recovery — trimming context and retrying on a client fault — was
rejected: it adds a trimming heuristic to a file whose job is to show two
models composing, and heuristics in an example get copied. Ignoring `fault`
entirely was also rejected: it is information the library goes out of its way
to deliver, and the thing an agent author most needs.

**When `fault is None`, one line records that the exposure figures are a floor
rather than a count** — the client lost contact and never learned who saw its
content. #59 established that distinction; an example printing exposure after
a timeout without it would quietly overclaim, which this project's standards
forbid.

### The test uses a frame-recording fake coordinator

The same shape `tests/client/test_flow.py` uses: a fake that records the raw
frames it received and replies with canned results. That allows the assertion
that matters — that hop 2's frame equals **exactly** the two messages the code
named. "Contains the draft" would pass against an implementation that also
resent hop 1.

A real coordinator with a fake node would additionally catch a wire-level
mismatch, but #61 is chartered to prove exactly that on real hardware against
real models, and duplicating it here would add runtime for little extra
signal.

The test loads the file with `importlib.util.spec_from_file_location`, path
resolved from the test's own location — no `sys.path` mutation leaking into
other tests, and `examples/` stays genuinely outside the installed package.

The fake-coordinator helpers are **duplicated** into the new test file rather
than extracted to a shared conftest. That follows this repo's established
precedent: `_client_ssl_context` and `_fake_identity_verifier` are already
copied across `test_cli.py`, `test_server.py` and `test_transport.py`.
Extracting them is a defensible refactor, but it is not this issue's work.

### Documentation

Module docstring plus `--help`, and one line in `docs/OPERATIONS.md`'s
client-library section pointing at the file. `examples/` has one file and is
meant to keep having one; a `README.md` would split the explanation across two
places that can drift.

## Out of scope

Any change to `src/mycelium/`. A second example. Retry or context-trimming
logic. `list_models`-based discovery. Real-hardware verification, which is
#61. ADR-0004's Consequences note, which is #57's.
