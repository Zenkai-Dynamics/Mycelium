"""CLI entry point for the Mycelium node agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from mycelium import __version__
from mycelium.node import connection
from mycelium.node.vllm_process import (
    DEFAULT_GPU,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    VLLMProcess,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-node")
    parser.add_argument("--coordinator-url", default=None)
    parser.add_argument("--coordinator-cert", type=Path, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gpu", default=DEFAULT_GPU)
    parser.add_argument("--vllm-port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--prompt",
        default=None,
        help="Send this one prompt to vLLM and exit, without connecting to a coordinator.",
    )
    args = parser.parse_args(argv)

    has_url = args.coordinator_url is not None
    has_cert = args.coordinator_cert is not None
    if has_url != has_cert:
        parser.error("--coordinator-url and --coordinator-cert must be given together")
    if not (has_url and has_cert) and args.prompt is None:
        parser.error("either --coordinator-url/--coordinator-cert or --prompt is required")

    return args


async def _run(args: argparse.Namespace) -> None:
    process = VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
    print(f"starting vLLM ({args.model} on GPU {args.gpu})...", flush=True)
    await asyncio.to_thread(process.start)
    try:
        await asyncio.to_thread(process.wait_ready)
        print("vLLM ready", flush=True)

        if args.prompt is not None:
            result = await asyncio.to_thread(process.complete, args.prompt)
            print(result, flush=True)
            return

        print(f"mycelium-node {__version__} connecting to {args.coordinator_url}", flush=True)
        async for websocket in connection.connect(args.coordinator_url, args.coordinator_cert):
            print(f"connected to coordinator ({args.coordinator_url})", flush=True)
            try:
                await websocket.wait_closed()
                print("connection to coordinator closed, reconnecting...", flush=True)
            except Exception:
                continue
    finally:
        await asyncio.to_thread(process.stop)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
