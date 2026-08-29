"""CLI entry point for the Mycelium node agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
import webbrowser
from pathlib import Path

from mycelium import __version__
from mycelium import crypto
from mycelium.node import connection, github_device_flow, identity, registration, request_handler
from mycelium.node.vllm_process import (
    DEFAULT_GPU,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    VLLMProcess,
    VLLMReadyTimeout,
)


DEFAULT_GITHUB_TOKEN_PATH = Path.home() / ".mycelium" / "github-token"

# Must match the exact rejection reason string server.py's
# _handle_registration sends for github_identity.InvalidGithubToken (see
# coordinator/server.py) — a single, deliberate string-match coupling
# used only to print a more useful diagnostic hint on retry, not to
# change control flow. See the design doc for issue #34.
_STALE_GITHUB_TOKEN_REASON = "invalid or expired GitHub token"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-node")
    parser.add_argument("--coordinator-url", default=None)
    parser.add_argument("--coordinator-cert", type=Path, default=None)
    parser.add_argument("--github-token-file", type=Path, default=None)
    parser.add_argument("--node-id", default=None)
    parser.add_argument("--node-key-file", type=Path, default=identity.DEFAULT_KEY_PATH)
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


async def _request_and_print_code(client, open_browser=webbrowser.open) -> github_device_flow.DeviceCode:
    """Request a fresh device code, print the volunteer-facing
    instructions, and best-effort open a browser to the verification URL.
    Raises SystemExit if CLIENT_ID is still the shipped placeholder — see
    the design doc for issue #34."""
    try:
        device = await asyncio.to_thread(client.request_device_code)
    except github_device_flow.DeviceFlowConfigError as exc:
        raise SystemExit(f"error: {exc}")
    print(f"First copy your one-time code: {device.user_code}", flush=True)
    print(f"Then visit: {device.verification_uri} and enter it", flush=True)
    try:
        open_browser(device.verification_uri)
    except Exception:
        pass
    return device


async def _authenticate(client=github_device_flow, sleep=asyncio.sleep, open_browser=webbrowser.open) -> str:
    """Drive GitHub's OAuth device flow to completion and return the
    resulting access token. Only ever called when no usable GitHub token
    was found on disk — see _run(). See the design doc for issue #34 for
    the full polling-error decision table."""
    device = await _request_and_print_code(client, open_browser)
    interval = device.interval
    while True:
        await sleep(interval)
        try:
            result = await asyncio.to_thread(client.poll_once, device.device_code, interval)
        except github_device_flow.DeviceCodeExpired:
            print("Code expired, requesting a new one...", flush=True)
            device = await _request_and_print_code(client, open_browser)
            interval = device.interval
            continue
        except github_device_flow.AuthorizationDenied:
            raise SystemExit("GitHub sign-in was denied. Re-run mycelium-node to try again.")
        except github_device_flow.DeviceFlowConfigError as exc:
            raise SystemExit(f"error: {exc}")
        interval = result.interval
        if result.token is not None:
            return result.token


async def _run(
    args: argparse.Namespace,
    process: VLLMProcess,
    default_github_token_path: Path = DEFAULT_GITHUB_TOKEN_PATH,
    device_flow_client=github_device_flow,
    device_flow_sleep=asyncio.sleep,
    device_flow_open_browser=webbrowser.open,
) -> None:
    node_id = None
    public_key = None
    signature = None
    github_token = None
    if args.prompt is None:
        node_id = args.node_id or socket.gethostname()
        private_key = identity.load_or_create_keypair(args.node_key_file)
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)

        if args.github_token_file is not None:
            # Explicitly passed — a missing file is a hard error, never a
            # fallback into the interactive device flow (see the design
            # doc for issue #34: a typo'd path on an unattended box must
            # not silently hang printing a device code nobody's watching
            # for).
            if not args.github_token_file.exists():
                raise SystemExit(
                    f"--github-token-file at {args.github_token_file} does not exist"
                )
            github_token = args.github_token_file.read_text().strip()
            if not github_token:
                raise SystemExit(f"--github-token-file at {args.github_token_file} is empty")
        elif default_github_token_path.exists():
            github_token = default_github_token_path.read_text().strip()
            if not github_token:
                raise SystemExit(f"{default_github_token_path} is empty")
        else:
            github_token = await _authenticate(
                device_flow_client, device_flow_sleep, device_flow_open_browser
            )
            default_github_token_path.parent.mkdir(parents=True, exist_ok=True)
            default_github_token_path.write_text(github_token)
            default_github_token_path.chmod(0o600)

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
        registration_backoff = connection.reconnect_delays()
        async for websocket in connection.connect(args.coordinator_url, args.coordinator_cert):
            print(f"connected to coordinator ({args.coordinator_url})", flush=True)
            try:
                await registration.register(
                    websocket, model=args.model, node_id=node_id,
                    public_key=public_key, signature=signature, github_token=github_token,
                )
                print(f"registered with coordinator as {node_id!r}", flush=True)
                registration_backoff = connection.reconnect_delays()  # reset after success
            except registration.RegistrationError as exc:
                delay = next(registration_backoff)
                hint = (
                    f" (this looks like a stale GitHub token — delete "
                    f"{default_github_token_path} to re-authenticate)"
                    if str(exc) == _STALE_GITHUB_TOKEN_REASON else ""
                )
                print(f"registration failed: {exc}; retrying in {delay:.1f}s{hint}", flush=True)
                await websocket.close()
                await asyncio.sleep(delay)
                continue
            try:
                await request_handler.handle_messages(websocket, process)
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
