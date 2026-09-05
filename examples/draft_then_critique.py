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

from pathlib import Path

from mycelium.client import Flow, assistant, user

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
