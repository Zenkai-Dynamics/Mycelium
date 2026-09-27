"""CLI entry point for asking the coordinator which models are served.

A separate entry point rather than a subcommand of mycelium-client,
matching the mycelium-coordinator-status / -ban precedent. See the design
doc for issue #56.

Unlike the operator CLIs, this one goes through mycelium.client.transport
— the shared round trip extracted in issue #59 — rather than hand-rolling
connect-send-receive against a wire three issues have already changed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mycelium.client import transport

# Kept equal to flow.DISCOVERY_TIMEOUT_SECONDS and pinned by a test, the
# same way cli.CLIENT_COMPLETE_TIMEOUT_SECONDS is pinned against
# flow.CALL_TIMEOUT_SECONDS: a registry lookup never reaches a node, so
# there is no per-attempt timeout underneath it to sit behind — the
# coordinator either answers quickly or it is not going to (issue #56).
LIST_MODELS_TIMEOUT_SECONDS = 10.0


class QueryError(Exception):
    """Raised when the discovery request fails: the coordinator rejected it
    (a wrong token closes the connection without replying), it could not be
    reached, or it answered with something unexpected."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-client-models")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    return parser.parse_args(argv)


async def list_models(
    coordinator_url: str,
    coordinator_cert: Path,
    token: str,
    timeout: float = LIST_MODELS_TIMEOUT_SECONDS,
) -> list[dict]:
    """Ask the coordinator which models are currently served.

    Advisory only: a node can disconnect between this answer and a later
    completion, so the result is a snapshot, never a guarantee.
    """
    try:
        reply = await transport.request(
            coordinator_url, coordinator_cert,
            {"type": "list_models", "token": token}, timeout,
        )
    except transport.TransportError as exc:
        raise QueryError(str(exc)) from None
    models = reply.get("models") if reply.get("type") == "models" else None
    if not isinstance(models, list) or any(
        not isinstance(entry, dict) or "model" not in entry or "healthy_nodes" not in entry
        for entry in models
    ):
        raise QueryError(f"unexpected response from coordinator: {reply!r}")
    return models


def main() -> None:
    args = parse_args()
    try:
        token = args.token_file.read_text().strip()
        models = asyncio.run(
            list_models(args.coordinator_url, args.coordinator_cert, token)
        )
    except (OSError, QueryError) as exc:
        # OSError covers a missing, unreadable, or directory-as-path token
        # file (FileNotFoundError/PermissionError/IsADirectoryError are all
        # subclasses) — that read must fail the same clean way as a
        # rejected token, not escape as a traceback.
        print(f"error: {exc}", flush=True)
        sys.exit(1)
    if not models:
        print("No models currently served.")
        return
    for entry in models:
        nodes = entry["healthy_nodes"]
        print(f"{entry['model']}  {nodes} node{'' if nodes == 1 else 's'}")


if __name__ == "__main__":
    main()
