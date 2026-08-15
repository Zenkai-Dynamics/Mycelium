"""CLI entry point for the Mycelium node agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
from pathlib import Path

from mycelium import __version__
from mycelium.node import connection, registration
from mycelium.node.vllm_process import (
    DEFAULT_GPU,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    VLLMProcess,
    VLLMReadyTimeout,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-node")
    parser.add_argument("--coordinator-url", default=None)
    parser.add_argument("--coordinator-cert", type=Path, default=None)
    parser.add_argument("--token-file", type=Path, default=None)
    parser.add_argument("--node-id", default=None)
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
    if has_url and args.token_file is None:
        parser.error("--token-file is required when connecting to a coordinator")

    return args


async def _run(args: argparse.Namespace, process: VLLMProcess) -> None:
    token = None
    node_id = None
    if args.prompt is None:
        token = args.token_file.read_text().strip()
        if not token:
            raise SystemExit(f"--token-file at {args.token_file} is empty")
        node_id = args.node_id or socket.gethostname()

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
                await registration.register(websocket, token=token, model=args.model, node_id=node_id)
                print(f"registered with coordinator as {node_id!r}", flush=True)
            except registration.RegistrationError as exc:
                print(f"registration failed: {exc}", flush=True)
                await websocket.close()
                continue
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
    process = VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    # Raw signal.signal, not asyncio's loop.add_signal_handler: a real OS
    # signal interrupts the event loop's blocking wait even while the main
    # coroutine is stuck inside an `asyncio.to_thread(...)` call (e.g. mid
    # `wait_ready`, which can block up to READY_TIMEOUT_SECONDS) — asyncio
    # task cancellation cannot interrupt an already-running executor thread,
    # so only a synchronous, process-level handler reliably stops vLLM here.
    def _handle_signal(signum: int, _frame) -> None:
        process.stop()
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    try:
        asyncio.run(_run(args, process))
    except VLLMReadyTimeout as exc:
        print(f"error: {exc}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
