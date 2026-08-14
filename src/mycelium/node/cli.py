"""CLI entry point for the Mycelium node agent."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mycelium import __version__
from mycelium.node import connection


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-node")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    print(f"mycelium-node {__version__} connecting to {args.coordinator_url}")
    async for websocket in connection.connect(args.coordinator_url, args.coordinator_cert):
        print(f"connected to coordinator ({args.coordinator_url})")
        try:
            await websocket.wait_closed()
            print("connection to coordinator closed, reconnecting...")
        except Exception:
            continue


def main() -> None:
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
