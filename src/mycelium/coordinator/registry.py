"""In-memory registry of currently-registered nodes.

See the design doc for issue #8. Tracks which nodes are registered, the
model each hosts, and the live connection to reach them. Mutated only
from the coordinator's single asyncio event loop — no locking needed,
since plain dict operations don't yield control mid-mutation.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any


@dataclass
class Node:
    node_id: str
    model: str
    websocket: Any


class NodeRegistry:
    """Holds the shared token and the current set of registered nodes."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._nodes: dict[str, Node] = {}

    def check_token(self, token: str) -> bool:
        return hmac.compare_digest(token, self._token)

    def register(self, node_id: str, model: str, websocket: Any) -> Node | None:
        """Add or replace node_id's entry. Returns the superseded Node if
        one existed under this node_id, else None — the caller is
        responsible for closing the superseded connection."""
        previous = self._nodes.get(node_id)
        self._nodes[node_id] = Node(node_id=node_id, model=model, websocket=websocket)
        return previous

    def unregister(self, node_id: str, websocket: Any) -> None:
        """Remove node_id's entry, but only if it's still this exact
        connection — a newer registration may have already replaced it."""
        current = self._nodes.get(node_id)
        if current is not None and current.websocket is websocket:
            del self._nodes[node_id]

    def list_nodes(self) -> list[dict]:
        return [{"node_id": n.node_id, "model": n.model} for n in self._nodes.values()]
