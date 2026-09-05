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
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    draft_model: str,
    critique_model: str,
    task: str,
    flow: Flow | None = None,
) -> Flow:
    """Draft with one model, improve with another, and return the Flow.

    Returns rather than prints so the flow can be inspected — by main()
    below, and by the tests. A failed hop propagates as HopError; deciding
    what to do about it belongs to the caller, not here.

    `flow` lets the caller own the record. That matters on failure: this
    function raises rather than returning, so a caller that did not
    already hold the Flow would lose the history of everything that
    happened before the failure — including which volunteers had already
    seen content. Passing one in keeps that record in the caller's hands,
    which is what issue #59 built it for.
    """
    flow = flow or Flow(coordinator_url, coordinator_cert, token)

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
    parser.add_argument("--task", default=DEFAULT_TASK)
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


def _print_exposure(flow: Flow, contact_lost: bool) -> None:
    exposure = flow.exposure()
    print("\nwho saw what:")
    for handle, hops in exposure.by_node.items():
        print(f"  node {handle} saw hops {hops}")
    for handle, hops in exposure.by_identity.items():
        who = "an unresolved identity" if handle is None else f"identity {handle}"
        print(f"  {who} saw hops {hops}")
    if contact_lost:
        # The client never learned who served the failed hop, so anyone
        # the coordinator had already routed to is missing from the count.
        # See the design doc for issue #59 on why this is a floor.
        print(
            "\n  note: contact was lost mid-flow, so these figures are a "
            "floor, not a count — the coordinator may already have sent "
            "your content to a volunteer this client never heard about."
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
        asyncio.run(run(
            args.coordinator_url, args.coordinator_cert, token,
            args.draft_model, args.critique_model, args.task, flow,
        ))
    except HopError as exc:
        print(f"\nhop failed: {exc.reason}")
        if exc.fault == "client":
            print("  fault: client — the request was the problem, not the node")
        elif exc.fault == "node":
            print("  fault: node — the volunteer's machine failed, not your request")
        else:
            print("  fault: unknown — the request never reached a node")
        _print_hops(flow)
        _print_exposure(flow, contact_lost=exc.fault is None)
        return 1

    _print_hops(flow)
    _print_exposure(flow, contact_lost=False)
    print(f"\nresult:\n{flow.hops[-1].text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
