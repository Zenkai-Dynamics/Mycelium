"""CLI entry point for the Mycelium client.

See the design doc for issue #10. A one-shot request: connect to the
coordinator like mycelium-coordinator-status does, send exactly one
"complete" message, print the result (or a clear error), exit — matching
the phase-0 doc's "a basic client interface to send a prompt and get a
completion back."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mycelium.client import transport

# 10s past coordinator/router.py's per-attempt
# NODE_COMPLETE_TIMEOUT_SECONDS (130s), so on a single attempt the
# coordinator's timeout fires first and this client gets that specific
# complete_error reason, rather than giving up first with a vaguer
# "coordinator did not respond" message of its own. Across a failover it
# does not hold — the coordinator's retry loop has no overall budget, so
# each new node gets a fresh 130s. Kept equal to flow.CALL_TIMEOUT_SECONDS
# and pinned by a test (issue #59).
CLIENT_COMPLETE_TIMEOUT_SECONDS = 140.0


class CompletionError(Exception):
    """Raised when the coordinator rejects the request, the routed node
    fails or is unavailable, or no response arrives in time."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-client")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    # Mutually exclusive and required: the wire rejects both-set as
    # ambiguous (issue #55), so the CLI refuses to build such a request
    # in the first place rather than making the user learn that by
    # round-tripping.
    content = parser.add_mutually_exclusive_group(required=True)
    content.add_argument("--prompt")
    content.add_argument(
        "--messages-file",
        type=Path,
        help="path to a JSON file holding a [{\"role\": ..., \"content\": ...}, ...] array",
    )
    return parser.parse_args(argv)


async def complete(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    model: str,
    prompt: str | None = None,
    messages: list[dict] | None = None,
    timeout: float = CLIENT_COMPLETE_TIMEOUT_SECONDS,
) -> str:
    """Send one prompt or conversation to the coordinator and return the
    completion text.

    Exactly one of `prompt` or `messages` must be given — the same rule the
    wire enforces (see the design doc for issue #55), checked here so an
    ambiguous request fails locally instead of after a round trip.

    Raises CompletionError if the request is ambiguous, the coordinator
    rejects it, the routed node fails or is unavailable, or no response
    arrives in time.
    """
    if (prompt is None) == (messages is None):
        raise CompletionError("send either prompt or messages, not both or neither")

    request = {"type": "complete", "token": token, "model": model}
    if prompt is not None:
        request["prompt"] = prompt
    else:
        request["messages"] = messages

    try:
        message = await transport.request(coordinator_url, coordinator_cert, request, timeout)
    except transport.TransportError as exc:
        # transport doesn't know this caller has a --token-file flag, so
        # its "(check the token)" wording is kept generic (issue #59). The
        # CLI knows more about its own likely cause and re-raises with the
        # original, more specific text rather than transport's.
        if "closed the connection without responding" in str(exc):
            raise CompletionError(
                "coordinator closed the connection without responding (check --token-file)"
            ) from None
        raise CompletionError(str(exc)) from None

    if message.get("type") == "complete_result":
        return message["text"]
    if message.get("type") == "complete_error":
        raise CompletionError(message.get("reason", "unknown reason"))
    raise CompletionError(f"unexpected response from coordinator: {message!r}")


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    messages = None
    if args.messages_file is not None:
        try:
            messages = json.loads(args.messages_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read {args.messages_file}: {exc}", flush=True)
            sys.exit(1)
        if messages is None:
            # A file holding the JSON literal `null` parses without
            # raising, leaving `messages` indistinguishable from "the flag
            # was never given" downstream — caught here so the user sees
            # what's actually wrong instead of complete()'s generic
            # "not both or neither" flag error (issue #55).
            print(
                f"error: {args.messages_file} parsed to null, expected a "
                "messages array",
                flush=True,
            )
            sys.exit(1)
    try:
        text = asyncio.run(
            complete(
                args.coordinator_url, args.coordinator_cert, token, args.model,
                prompt=args.prompt, messages=messages,
            )
        )
    except CompletionError as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    print(text, flush=True)


if __name__ == "__main__":
    main()
