"""CLI entry point for querying the coordinator's node registry.

See the design doc for issue #8. Connects like a node would (same TLS
cert, same shared token) but as a one-shot request/response, not a
long-lived connection — this is an operator tool, not a node agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

from mycelium.node.connection import build_ssl_context

STATUS_QUERY_TIMEOUT_SECONDS = 10.0


class QueryError(Exception):
    """Raised when the status query fails: the coordinator rejected it
    (wrong/missing token) or didn't respond in time."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-coordinator-status")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    return parser.parse_args(argv)


async def query_status(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    timeout: float = STATUS_QUERY_TIMEOUT_SECONDS,
) -> list[dict]:
    """Connect, ask for the current registry, and return the node list.

    Raises QueryError if the coordinator rejects the query (e.g. a wrong
    token — it closes without replying) or doesn't respond in time."""
    ssl_context = build_ssl_context(coordinator_cert)
    async with websockets.connect(coordinator_url, ssl=ssl_context) as websocket:
        await websocket.send(json.dumps({"type": "status_query", "token": token}))
        try:
            async with asyncio.timeout(timeout):
                raw = await websocket.recv()
        except TimeoutError:
            raise QueryError(f"coordinator did not respond within {timeout}s") from None
        except websockets.exceptions.ConnectionClosed:
            raise QueryError(
                "coordinator rejected the status query (check --token-file)"
            ) from None
        message = json.loads(raw)
        return message.get("nodes", [])


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    try:
        nodes = asyncio.run(query_status(args.coordinator_url, args.coordinator_cert, token))
    except QueryError as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    if not nodes:
        print("No nodes registered.")
        return
    for node in nodes:
        print(f"{node['node_id']}: {node['model']}")


if __name__ == "__main__":
    main()
