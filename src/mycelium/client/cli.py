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

import websockets

from mycelium.node.connection import build_ssl_context

# 10s past coordinator/router.py's own NODE_COMPLETE_TIMEOUT_SECONDS
# (130s), so the coordinator's timeout fires first and this client gets
# that specific complete_error reason, rather than giving up first with a
# vaguer "coordinator did not respond" message of its own.
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
    parser.add_argument("--prompt", required=True)
    return parser.parse_args(argv)


async def complete(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    model: str,
    prompt: str,
    timeout: float = CLIENT_COMPLETE_TIMEOUT_SECONDS,
) -> str:
    """Send one prompt to the coordinator and return the completion text.

    Raises CompletionError if the coordinator rejects the request, the
    routed node fails or is unavailable, or no response arrives in time.
    """
    ssl_context = build_ssl_context(coordinator_cert)
    async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
        await websocket.send(json.dumps(
            {"type": "complete", "token": token, "model": model, "prompt": prompt}
        ))
        try:
            async with asyncio.timeout(timeout):
                raw = await websocket.recv()
        except TimeoutError:
            raise CompletionError(f"coordinator did not respond within {timeout}s") from None
        except websockets.exceptions.ConnectionClosed:
            raise CompletionError(
                "coordinator closed the connection without responding (check --token-file)"
            ) from None

        message = json.loads(raw)
        if message.get("type") == "complete_result":
            return message["text"]
        if message.get("type") == "complete_error":
            raise CompletionError(message.get("reason", "unknown reason"))
        raise CompletionError(f"unexpected response from coordinator: {message!r}")


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    try:
        text = asyncio.run(
            complete(args.coordinator_url, args.coordinator_cert, token, args.model, args.prompt)
        )
    except CompletionError as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    print(text, flush=True)


if __name__ == "__main__":
    main()
