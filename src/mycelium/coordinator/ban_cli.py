"""CLI entry point for banning a node's bound GitHub identity.

See the design doc for issue #37. Connects the same way
mycelium-coordinator-status does (same TLS cert, same shared token) as a
one-shot request/response — this is a manual operator override, not a
node agent. There is no unban command: ban state is in-memory only, like
every other piece of coordinator state, so a coordinator restart is the
only reset path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

from mycelium.node.connection import build_ssl_context

BAN_REQUEST_TIMEOUT_SECONDS = 10.0


class BanError(Exception):
    """Raised when the ban command fails: the coordinator rejected it
    (wrong/missing token, unknown identity) or didn't respond in time."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-coordinator-ban")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--identity", required=True)
    return parser.parse_args(argv)


async def ban_identity(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    identity: str,
    timeout: float = BAN_REQUEST_TIMEOUT_SECONDS,
) -> int:
    """Connect, ask the coordinator to ban `identity`, and return the
    number of currently-connected nodes it disconnected as a result.

    Raises BanError if the coordinator rejects the command (a wrong
    token — it closes without replying; an unknown identity — it replies
    with ban_failed) or doesn't respond in time."""
    ssl_context = build_ssl_context(coordinator_cert)
    async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
        await websocket.send(json.dumps(
            {"type": "ban_identity", "token": token, "identity": identity}
        ))
        try:
            async with asyncio.timeout(timeout):
                raw = await websocket.recv()
        except TimeoutError:
            raise BanError(f"coordinator did not respond within {timeout}s") from None
        except websockets.exceptions.ConnectionClosed:
            raise BanError(
                "coordinator rejected the ban command (check --token-file)"
            ) from None
        message = json.loads(raw)
        if message.get("type") == "ban_failed":
            raise BanError(message.get("reason", "ban failed"))
        return message.get("disconnected_count", 0)


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    try:
        disconnected_count = asyncio.run(
            ban_identity(args.coordinator_url, args.coordinator_cert, token, args.identity)
        )
    except BanError as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    print(
        f"banned {args.identity!r} — disconnected {disconnected_count} "
        f"currently-registered node(s)"
    )


if __name__ == "__main__":
    main()
