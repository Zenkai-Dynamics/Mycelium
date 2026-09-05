"""The primitive agents are written against: a Flow that carries one task
across several models.

There is deliberately no implicit accumulator. Conventional agent
libraries keep conversation state and resend it on every call; here that
would mean every volunteer in a flow sees everything that came before it.
Instead the Flow records each hop locally and sends only what that call
named — see ADR-0004. The property is structural, not a setting: there is
no code path here that sends unnamed content, so it cannot be left
switched off.

The record is never transmitted. It exists for the agent's own logic, for
debugging, and for the exposure report that makes ADR-0004's claim
checkable rather than merely stated. See the design doc for issue #59.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from pathlib import Path

from mycelium.client import transport

# Matches mycelium.client.cli's own default: 10s past the coordinator's
# NODE_COMPLETE_TIMEOUT_SECONDS, so the coordinator's timeout fires first
# and the caller gets its specific reason rather than a vaguer local one.
CALL_TIMEOUT_SECONDS = 140.0


def system(content: str) -> dict:
    """A system message. Exists so ADR-0004's documented example is
    runnable rather than pseudocode."""
    return {"role": "system", "content": content}


def user(content: str) -> dict:
    """A user message."""
    return {"role": "user", "content": content}


def assistant(content: str) -> dict:
    """An assistant message — most often a previous hop's output, passed
    forward deliberately."""
    return {"role": "assistant", "content": content}


@dataclass(frozen=True)
class Hop:
    """One request/response exchange with one node, as the client saw it.

    A failed hop is as complete a record as a successful one: `text` is
    None and `error` is set, but everything else — what was sent, who saw
    it, how long it took — is recorded the same way. Issue #59 requires
    the record to stay intact and usable after a failure.

    `elapsed_ms` is the coordinator's own measure of routing time (issue
    #63) and is None when no node was attempted; `wall_ms` is the whole
    round trip as the client experienced it. Their difference is the
    client-coordinator overhead issue #61 measures.

    `frozen=True` stops a Hop's own fields from being rebound (`hop.text =
    ...` raises) — it does not freeze what `sent` or `exposed` point to,
    since both are plain lists and a frozen dataclass only guards its own
    attributes, not the contents of mutable objects it holds. What is
    actually guaranteed is narrower and lives on `Flow.call`: `sent` is a
    deep copy taken at call time, so mutating the list the caller passed
    to `call` afterwards cannot rewrite this Hop. Reaching into
    `hop.sent` and editing it directly is still possible; nothing here
    stops that, because the hazard this defends against is an agent
    accidentally corrupting its own record by reusing and trimming a
    message list, not a hostile edit of the record after the fact.
    """

    index: int
    model: str
    sent: list[dict]
    text: str | None
    error: str | None
    fault: str | None
    exposed: list[dict]
    elapsed_ms: int | None
    wall_ms: int


class HopError(Exception):
    """A hop failed. Carries the node's reason, the `fault` that says whose
    mistake it was (issue #58 — "client" means fix the request, "node"
    means it may be worth trying elsewhere), and the recorded Hop, so a
    handler has the exposure and timing without reaching into flow.hops."""

    def __init__(self, reason: str, fault: str | None, hop: Hop) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fault = fault
        self.hop = hop


class Flow:
    """One logical task spanning several hops, typically across different
    models.

    Holds the coordinator's address and token, and the local record — and
    nothing else. In particular it holds no accumulated conversation:
    `call` sends exactly the messages it is given. An agent that wants a
    previous hop's output in a later hop passes it deliberately.
    """

    def __init__(self, coordinator_url: str, coordinator_cert: Path, token: str) -> None:
        self._coordinator_url = coordinator_url
        self._coordinator_cert = coordinator_cert
        self._token = token
        self._hops: list[Hop] = []
        self._next_index = 0

    @property
    def hops(self) -> list[Hop]:
        """Every hop that has finished, in the order the calls were made.

        Ordered by the index assigned when each call started, not by when
        it completed, so a flow that fans out with asyncio.gather still
        reads back in the agent's own order. A hop appears only once it has
        succeeded or failed — never half-populated.
        """
        return sorted(self._hops, key=lambda hop: hop.index)

    async def call(
        self, model: str, *, messages: list[dict], timeout: float = CALL_TIMEOUT_SECONDS
    ) -> Hop:
        """Send `messages` to `model` and return the recorded Hop.

        Only `messages` is sent. Nothing from earlier hops is added — see
        ADR-0004.

        The messages are deep-copied into the record, so an agent that
        reuses and trims its list across hops cannot retroactively rewrite
        what this hop is recorded as having sent. The record is the
        evidence behind the exposure report; a reference the caller can
        mutate would not be.

        Raises HopError if the hop fails, after recording it. Structural
        validation of `messages` is the coordinator's job (issue #55), not
        duplicated here.
        """
        index = self._next_index
        self._next_index += 1
        sent = copy.deepcopy(messages)
        request = {
            "type": "complete", "token": self._token, "model": model, "messages": messages,
        }

        started = time.monotonic()
        try:
            reply = await transport.request(
                self._coordinator_url, self._coordinator_cert, request, timeout
            )
        except transport.TransportError as exc:
            hop = Hop(
                index=index, model=model, sent=sent, text=None, error=str(exc), fault=None,
                exposed=[], elapsed_ms=None,
                wall_ms=int((time.monotonic() - started) * 1000),
            )
            self._hops.append(hop)
            raise HopError(str(exc), None, hop) from None

        wall_ms = int((time.monotonic() - started) * 1000)
        exposed = reply.get("exposed", [])
        elapsed_ms = reply.get("elapsed_ms")

        if reply.get("type") == "complete_result":
            hop = Hop(
                index=index, model=model, sent=sent, text=reply.get("text"), error=None,
                fault=None, exposed=exposed, elapsed_ms=elapsed_ms, wall_ms=wall_ms,
            )
            self._hops.append(hop)
            return hop

        reason = reply.get("reason", f"unexpected response from coordinator: {reply!r}")
        hop = Hop(
            index=index, model=model, sent=sent, text=None, error=reason,
            fault=reply.get("fault"), exposed=exposed, elapsed_ms=elapsed_ms, wall_ms=wall_ms,
        )
        self._hops.append(hop)
        raise HopError(reason, hop.fault, hop)
