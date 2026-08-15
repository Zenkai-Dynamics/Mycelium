"""In-memory registry of currently-registered nodes.

See the design doc for issue #8. Tracks which nodes are registered, the
model each hosts, and the live connection to reach them. Mutated only
from the coordinator's single asyncio event loop — no locking needed,
since plain dict operations don't yield control mid-mutation.
"""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    node_id: str
    model: str
    websocket: Any
    # In-flight routed requests on this node's connection, keyed by
    # request_id — see the design doc for issue #10. Lives on the Node
    # itself (not a separate coordinator-wide dict) so it's tied to this
    # exact connection's lifetime: when the connection goes away, whoever
    # cleans it up already has this dict via the Node reference they
    # captured at registration time.
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class NodeRegistry:
    """Holds the shared token and the current set of registered nodes."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("token must not be empty")
        self._token = token
        self._nodes: dict[str, Node] = {}

    def check_token(self, token: Any) -> bool:
        """Constant-time comparison against the configured token. Returns
        False (never raises) for a token that isn't a comparable str —
        e.g. a client sending {"token": null} or a non-ASCII value, both
        of which would otherwise raise inside hmac.compare_digest."""
        if not isinstance(token, str):
            return False
        try:
            return hmac.compare_digest(token, self._token)
        except TypeError:
            return False

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

    def find_node_for_model(self, model: str) -> Node | None:
        """Return the first registered node hosting `model`, or None. No
        load balancing across same-model nodes — see the design doc for
        issue #10: Phase 0 doesn't need fairness, just a healthy match."""
        for node in self._nodes.values():
            if node.model == model:
                return node
        return None

    def get(self, node_id: str) -> Node | None:
        """Return the currently-registered Node for node_id, or None.

        Used by the connection-handling task right after it calls
        register(), to capture a stable reference to its own entry for
        later cleanup — see the design doc for issue #10 on why that
        reference must be captured once and never re-fetched later: a
        later re-fetch could return a *different* connection's Node if
        this one has since been superseded by a reconnect under the same
        node_id.
        """
        return self._nodes.get(node_id)

    def list_nodes(self) -> list[dict]:
        return [{"node_id": n.node_id, "model": n.model} for n in self._nodes.values()]
