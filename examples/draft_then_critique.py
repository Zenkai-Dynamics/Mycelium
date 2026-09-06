"""Draft, then critique: one task across two models.

A small fast model writes a first answer; a larger one improves it. Both
models demonstrably contribute, and the second hop needs content from the
first — which is exactly the property this example exists to demonstrate.

Mycelium ships primitives plus this one demonstration, not an agent
framework. That is why this file lives in examples/ rather than in the
installed package: roles, planning and tool-calling abstractions belong
to something like LangGraph, not to an inference router. See the design
doc for issue #60.

**What each hop carries is a decision, not a default.** The critique hop
is given the original task as well as the draft, because a critic that
cannot see the task cannot judge whether the draft answers it. That means
the second volunteer sees the task too, and the exposure report this
example prints will say so. Explicit context (ADR-0004) does not mean
"send less" — it means decide per hop, and be able to see what you
decided. Nothing is sent that a call did not name.

**This script prints your prompt and both models' full output**, which is
the point — the explicit-context rule is only checkable if you can see
what was actually sent. It does mean the output is not safe to paste
somewhere public: a bug report pasted verbatim publishes your task, the
draft and the critique along with it.

Run it against your own nodes:

    python examples/draft_then_critique.py \\
        --coordinator-url wss://coordinator.example:8765 \\
        --coordinator-cert coordinator.pem \\
        --token-file client-token.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mycelium.client import Flow, HopError, assistant, user

# The pair issue #61 plans to run on real hardware: a ~3GB instruct model
# alongside the 7B already served there. Overridable, so the example runs
# against whatever a reader actually serves.
DEFAULT_DRAFT_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DEFAULT_CRITIQUE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_TASK = "Explain why the sky is blue, in two sentences."


async def run(
    flow: Flow,
    draft_model: str,
    critique_model: str,
    task: str,
) -> Flow:
    """Draft with one model, improve with another, and return the Flow.

    Returns rather than prints so the flow can be inspected — by main()
    below, and by the tests. A failed hop propagates as HopError; deciding
    what to do about it belongs to the caller, not here.

    The caller constructs the `Flow` and hands it over, rather than this
    function building one from a URL, a cert and a token. That is not a
    testing affordance: this function raises rather than returning on a
    failed hop, so a caller that did not already hold the Flow would lose
    the history of everything that happened before the failure —
    including which volunteers had already seen content. Keeping the
    record in the caller's hands is what issue #59 built it for.

    Taking the connection details *as well* was the earlier shape, and it
    was worse in two ways at once: on the path main() actually takes they
    were dead arguments, and a caller passing a Flow built against one
    coordinator plus a URL naming another would get no error. A file whose
    job is to teach the API should not offer two ways to say the same
    thing, one of which is ignored.
    """
    draft = await flow.call(draft_model, messages=[user(task)])

    # The task goes to the critique hop as well as the draft — deliberately.
    # A critic that cannot see the task cannot judge whether the draft
    # answers it. See the design doc for issue #60 on why this is the
    # honest demonstration of the explicit-context rule rather than a
    # violation of it.
    await flow.call(
        critique_model,
        messages=[user(f"Improve this answer to: {task}"), assistant(draft.text)],
    )

    return flow


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="draft_then_critique",
        description="Draft an answer with one model, improve it with another.",
        # Needed for --help to actually show the two model defaults below,
        # not just name the flags — argparse otherwise prints the default
        # only when a default AND a help string are both present.
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--draft-model", default=DEFAULT_DRAFT_MODEL, help="model that writes the first draft"
    )
    parser.add_argument(
        "--critique-model", default=DEFAULT_CRITIQUE_MODEL, help="model that improves the draft"
    )
    parser.add_argument(
        "--task", default=DEFAULT_TASK, help="the task both models work on"
    )
    return parser.parse_args(argv)


def _print_hops(flow: Flow) -> None:
    """Show what each hop actually sent, so the explicit-context rule is
    visible in practice rather than only described."""
    for hop in flow.hops:
        print(f"\nhop {hop.index} -> {hop.model}")
        for message in hop.sent:
            print(f"  sent {message['role']}: {message['content']}")
        if hop.text is not None:
            print(f"  got: {hop.text}")


def _print_exposure(flow: Flow, who_served_unknown: bool) -> None:
    """Print who saw what, and say so honestly when the client cannot know.

    The caveat is keyed off `elapsed_ms`, not off `fault`. `fault` answers
    a different question entirely (whose mistake it was), and keying on it
    made this example contradict itself: it printed "the coordinator may
    already have sent your content to a volunteer" three lines after
    naming the volunteer that saw it, because a node crash comes back with
    a populated `exposed` and no `fault` at all.

    `elapsed_ms` is the coordinator's measure of its own routing time, and
    its absence means the coordinator never timed a routing attempt for
    this hop. That covers three situations the client cannot tell apart:
    a refused or unreachable coordinator (nothing sent, true count zero),
    a reply saying no healthy node was available (nothing routed, true
    count zero), and a round trip that sent everything and got no reply
    back (true count unknown, possibly more than zero).

    So the note below says only what holds across all three — that this
    client did not learn who served the hop, and the figures are a floor.
    It is deliberately weaker than the code could be: "no reply arrived"
    would be false on the no-healthy-node path, where a full reply did
    arrive. Distinguishing them from the Hop alone is impossible today
    — all three give `elapsed_ms=None`, `exposed=[]` and `fault=None`, and
    only the reason string differs, which this project does not match on
    across a component boundary. Issue #71 is where the coordinator-side
    signal that would make this exact belongs.
    """
    exposure = flow.exposure()
    print("\nwho saw what:")
    for handle, hops in exposure.by_node.items():
        print(f"  node {handle} saw hops {hops}")
    for handle, hops in exposure.by_identity.items():
        who = "an unresolved identity" if handle is None else f"identity {handle}"
        print(f"  {who} saw hops {hops}")
    if who_served_unknown:
        # Claims neither that a reply arrived nor that one didn't, and
        # neither that the content was seen nor that it wasn't. All four
        # are possible on the paths this fires on (see above); a floor is
        # the strongest statement true on every one of them. See Hop's
        # docstring in the flow library, and the design doc for issue #59
        # on why this is a floor.
        print(
            "\n  note: this client never learned who — if anyone — served "
            "that hop, so read these figures as a floor rather than a count."
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.token_file.read_text().strip()

    # Constructed here, not inside run(), so the record survives a failed
    # hop: run() raises rather than returning, and without holding the
    # Flow already this function could not report which volunteers had
    # seen content before things went wrong. See the design doc for #60.
    flow = Flow(args.coordinator_url, args.coordinator_cert, token)

    try:
        asyncio.run(run(flow, args.draft_model, args.critique_model, args.task))
    except HopError as exc:
        print(f"\nhop failed: {exc.reason}")
        if exc.fault == "client":
            print("  fault: client — the request was the problem, not the node")
        elif exc.fault == "node":
            print("  fault: node — the volunteer's machine failed, not your request")
        else:
            # No `fault` field came back. Today that covers two unrelated
            # situations, which is why this says nothing about whether a
            # node was reached: a transport failure where no reply arrived
            # at all, and *every* node-side failure — crash, timeout, no
            # healthy node — because the coordinator's reject() only ever
            # sets `fault` for client faults. Issue #71 tracks giving
            # node-side failures their "node" fault; when it lands, the
            # second group moves to the branch above and this one narrows
            # to genuine transport failures. The exposure section below
            # says which of the two this was: it reports a count when a
            # reply arrived and a floor when none did.
            print("  fault: unattributed — nothing came back saying whose failure this was")
        _print_hops(flow)
        # `elapsed_ms` is the coordinator's own measure of routing time,
        # so its absence means no routing attempt was ever timed for this
        # hop — the client therefore cannot know who served it. That is
        # the condition the floor caveat is about; see _print_exposure.
        _print_exposure(flow, who_served_unknown=exc.hop.elapsed_ms is None)
        return 1

    _print_hops(flow)
    _print_exposure(flow, who_served_unknown=False)
    print(f"\nresult:\n{flow.hops[-1].text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
