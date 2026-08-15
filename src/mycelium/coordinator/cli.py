"""CLI entry point for the Mycelium coordinator."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from mycelium import __version__
from mycelium.coordinator import certs, server

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_CERT_PATH = Path.home() / ".mycelium" / "coordinator-cert.pem"
DEFAULT_KEY_PATH = Path.home() / ".mycelium" / "coordinator-key.pem"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-coordinator")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cert-file", type=Path, default=DEFAULT_CERT_PATH)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_PATH)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--cert-san-ip",
        default=None,
        help=(
            "IP address to embed in the auto-generated cert's Subject "
            "Alternative Name. Required the first time, when --cert-file/"
            "--key-file don't exist yet."
        ),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    if not (args.cert_file.exists() and args.key_file.exists()):
        if not args.cert_san_ip:
            raise SystemExit(
                "--cert-san-ip is required to generate a new cert "
                f"(no existing cert found at {args.cert_file})"
            )
        certs.ensure_cert(args.cert_file, args.key_file, args.cert_san_ip)

    token = args.token_file.read_text().strip()
    if not token:
        raise SystemExit(f"--token-file at {args.token_file} is empty")

    print(
        f"mycelium-coordinator {__version__} listening on {args.host}:{args.port}",
        flush=True,
    )
    async with server.serve(args.host, args.port, args.cert_file, args.key_file, token):
        await asyncio.Future()  # run forever


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
