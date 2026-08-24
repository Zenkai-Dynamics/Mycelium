"""In-memory registry of currently-registered nodes.

See the design doc for issue #8, and for the re-keying to the node's
public key, the design doc for issue #33. Tracks which nodes are
registered, the model each hosts, and the live connection to reach them.
Mutated only from the coordinator's single asyncio event loop — no
locking needed, since plain dict operations don't yield control
mid-mutation.
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mycelium import crypto
from mycelium.coordinator import github_identity


@dataclass
class Node:
    # The node's on-the-wire identity (base64 Ed25519 public key) — see
    # the design doc for issue #33. This, not node_id, is what the
    # registry keys on and what a reconnect is recognized by.
    public_key: str
    # A human-readable label only, as of issue #33 — no longer unique,
    # no longer a supersede/hijack vector. Two different public keys may
    # share the same node_id without colliding.
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


@dataclass
class ReputationCounters:
    # Incremented at the exact points router.NodeTimeoutError /
    # router.NodeError / router.NodeDisconnectedError are already raised
    # (#10/#11), plus a successful completion. Never reset by
    # register()/unregister() — see the design doc for issue #36 on why
    # this must survive a node's disconnect/reconnect cycle to mean
    # anything (the disconnect counter especially: the connection that
    # triggers it is the one about to be unregistered).
    completions: int = 0
    timeouts: int = 0
    crashes: int = 0
    disconnects: int = 0


class MissingGithubToken(Exception):
    """Raised by NodeRegistry.resolve_identity when public_key has no
    bound identity yet and no github_token was supplied to establish
    one. See the design doc for issue #39."""


def _reputation_dict(counters: ReputationCounters | None) -> dict:
    counters = counters or ReputationCounters()
    return {
        "completions": counters.completions,
        "timeouts": counters.timeouts,
        "crashes": counters.crashes,
        "disconnects": counters.disconnects,
    }


class NodeRegistry:
    """Holds the shared token and the current set of registered nodes."""

    def __init__(
        self,
        token: str,
        identity_verifier: Callable[[str], Awaitable[github_identity.GithubIdentity]] | None = None,
    ) -> None:
        if not token:
            raise ValueError("token must not be empty")
        self._token = token
        self._nodes: dict[str, Node] = {}  # keyed by public_key
        # model -> public_key this returned last, for round-robin
        # selection in find_node_for_model. See the design doc for issue
        # #11 (mechanism) and issue #33 (keyed by public_key, not node_id).
        self._last_returned: dict[str, str] = {}
        # public_key -> the GithubIdentity bound to it, in-memory only —
        # see the design doc for issue #39 on why this deliberately does
        # not survive a coordinator restart.
        self._identity_by_key: dict[str, github_identity.GithubIdentity] = {}
        self._identity_verifier = identity_verifier or github_identity.verify_identity
        # public_key -> reputation counters, in-memory only, surviving
        # independently of _nodes — see the design doc for issue #36.
        self._reputation: dict[str, ReputationCounters] = {}

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

    async def resolve_identity(
        self, public_key: str, github_token: str | None
    ) -> github_identity.GithubIdentity:
        """Return the GithubIdentity bound to public_key. If public_key
        is already bound (an earlier call this coordinator process has
        seen), returns it immediately and never re-verifies —
        github_token is ignored entirely in that case, even if supplied.
        Otherwise github_token is required: raises MissingGithubToken if
        it's absent/empty, or propagates whatever
        github_identity.IdentityVerificationError subclass the injected
        verifier raises if verification fails. See the design doc for
        issue #39 on why a bound identity is never re-checked: a
        reconnect must not cost a GitHub API call, by design."""
        existing = self._identity_by_key.get(public_key)
        if existing is not None:
            return existing
        if not github_token:
            raise MissingGithubToken("github_token is required for first-time registration")
        identity = await self._identity_verifier(github_token)
        self._identity_by_key[public_key] = identity
        return identity

    def register(self, public_key: str, node_id: str, model: str, websocket: Any) -> Node | None:
        """Add or replace public_key's entry. Returns the superseded Node
        if one existed under this exact public_key, else None — the
        caller is responsible for closing the superseded connection. Two
        different public_keys sharing the same node_id do not collide —
        see the design doc for issue #33."""
        previous = self._nodes.get(public_key)
        self._nodes[public_key] = Node(
            public_key=public_key, node_id=node_id, model=model, websocket=websocket
        )
        return previous

    def unregister(self, public_key: str, websocket: Any) -> None:
        """Remove public_key's entry, but only if it's still this exact
        connection — a newer registration may have already replaced it."""
        current = self._nodes.get(public_key)
        if current is not None and current.websocket is websocket:
            del self._nodes[public_key]

    def record_completion(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).completions += 1

    def record_timeout(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).timeouts += 1

    def record_crash(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).crashes += 1

    def record_disconnect(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).disconnects += 1

    def find_node_for_model(
        self, model: str, exclude: frozenset[str] = frozenset()
    ) -> Node | None:
        """Return the next registered node hosting `model`, round-robin
        across current candidates (skipping any public_key in `exclude`),
        or None if none match — see the design doc for issue #11.

        Rotation state is the public_key this returned last time for
        `model`; the next call returns the candidate after it, wrapping
        around. If that node has since left the registry (or is itself
        excluded), rotation restarts from the front of the current
        candidate list — no fairness guarantee across registry churn,
        only "don't always pick the same node when several are healthy."

        `exclude` lets a caller retry with a different node within one
        client request (see server.py's failover loop) without disturbing
        the cross-request rotation state.
        """
        candidates = [
            node
            for node in self._nodes.values()
            if node.model == model and node.public_key not in exclude
        ]
        if not candidates:
            return None

        start = 0
        last = self._last_returned.get(model)
        if last is not None:
            keys = [node.public_key for node in candidates]
            if last in keys:
                start = (keys.index(last) + 1) % len(candidates)

        node = candidates[start]
        if not exclude:
            self._last_returned[model] = node.public_key
        return node

    def get(self, public_key: str) -> Node | None:
        """Return the currently-registered Node for public_key, or None.

        Used by the connection-handling task right after it calls
        register(), to capture a stable reference to its own entry for
        later cleanup — see the design doc for issue #10 on why that
        reference must be captured once and never re-fetched later: a
        later re-fetch could return a *different* connection's Node if
        this one has since been superseded by a reconnect under the same
        public_key.
        """
        return self._nodes.get(public_key)

    def list_nodes(self) -> list[dict]:
        return [
            {
                "node_id": n.node_id,
                "model": n.model,
                "fingerprint": crypto.fingerprint(n.public_key),
                "identity": (
                    self._identity_by_key[n.public_key].login
                    if n.public_key in self._identity_by_key
                    else None
                ),
                "reputation": _reputation_dict(self._reputation.get(n.public_key)),
            }
            for n in self._nodes.values()
        ]
