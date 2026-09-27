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
import random
import secrets
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
    # Times this node claimed a failure was the CLIENT's fault (issue
    # #58). Deliberately NOT part of _reputation_weight: a client's
    # mistake must never move routing, which is the bug #58 fixes. It is
    # counted at all because the claim is self-reported by an untrusted
    # volunteer — a node that always claims "client" would otherwise
    # accrue no signal whatsoever. This makes the claim observable to the
    # operator without letting it influence anything.
    client_faults: int = 0


class MissingGithubToken(Exception):
    """Raised by NodeRegistry.resolve_identity when public_key has no
    bound identity yet and no github_token was supplied to establish
    one. See the design doc for issue #39."""


class IdentityCapReached(Exception):
    """Raised by NodeRegistry.enforce_identity_cap when registering
    public_key as a NEW slot would push its identity's currently-
    registered node count to or past the configured per-identity cap.
    See the design doc for issue #35."""


class UnknownIdentity(Exception):
    """Raised by NodeRegistry.ban_identity when the operator asks to ban
    a GitHub login the coordinator has never seen bind to any public key.
    See the design doc for issue #37."""


class IdentityBanned(Exception):
    """Raised by NodeRegistry.enforce_not_banned when a registration
    attempt's resolved identity has been banned by the operator. See the
    design doc for issue #37."""


def _reputation_dict(counters: ReputationCounters | None) -> dict:
    counters = counters or ReputationCounters()
    return {
        "completions": counters.completions,
        "timeouts": counters.timeouts,
        "crashes": counters.crashes,
        "disconnects": counters.disconnects,
        "client_faults": counters.client_faults,
    }


class NodeRegistry:
    """Holds the shared token and the current set of registered nodes."""

    def __init__(
        self,
        token: str,
        identity_verifier: Callable[[str], Awaitable[github_identity.GithubIdentity]] | None = None,
        per_identity_cap: int = 3,
        random_source: random.Random | None = None,
        handle_secret: bytes | None = None,
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
        # Sybil-resistance cap — see the design doc for issue #35. A
        # literal default (not the None-sentinel-with-`or` pattern
        # identity_verifier uses above): 0 is a legitimate, meaningful
        # cap value ("freeze new registrations for every identity"),
        # which `0 or 3` would silently replace with 3.
        self._per_identity_cap = per_identity_cap
        # GithubIdentity.id values banned by the operator, in-memory only
        # — see the design doc for issue #37 on why there's no unban
        # command (a coordinator restart is the only reset path). Keyed
        # on the stable numeric id, never the login: a login is what the
        # operator types, but it isn't immutable across GitHub username
        # renames the way id is.
        self._banned_identity_ids: set[str] = set()
        # public_key -> reputation counters, in-memory only, surviving
        # independently of _nodes — see the design doc for issue #36.
        self._reputation: dict[str, ReputationCounters] = {}
        # A private instance, never the random module's shared global
        # functions — see the design doc for issue #36 on why (nothing
        # else in the process can perturb this registry's weighted draws
        # by calling random.seed() elsewhere). Tests inject
        # random.Random(<fixed seed>) for reproducible draws.
        self._random = random_source or random.Random()
        # Per-process and in-memory, exactly like the identity bindings,
        # bans and reputation counters above — so handles are not
        # comparable across a coordinator restart. That costs nothing: a
        # flow lives in one client process. Persisting it would create
        # the first on-disk artifact capable of linking one client's
        # flows across time. Injectable for tests only (see
        # identity_verifier and random_source for the same pattern);
        # production callers take the random default. Deliberately NOT
        # derived from the shared token — clients hold that, and could
        # then resolve every handle. See the design doc for issue #63.
        self._handle_secret = handle_secret or secrets.token_bytes(32)

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

    def enforce_not_banned(self, identity: github_identity.GithubIdentity) -> None:
        """Raise IdentityBanned if identity has been banned by the
        operator. Unlike enforce_identity_cap, there is NO exemption for
        a public_key that's already registered — a banned identity's
        reconnect attempts using an existing key must be rejected too.
        See the design doc for issue #37."""
        if identity.id in self._banned_identity_ids:
            raise IdentityBanned("this identity has been banned by the operator")

    def enforce_identity_cap(
        self, public_key: str, identity: github_identity.GithubIdentity
    ) -> None:
        """Raise IdentityCapReached if registering public_key as a NEW
        slot would push identity's currently-registered node count to or
        past the configured cap. A public_key already present in _nodes
        is exempt — it's a reconnect/replace of an existing slot (handled
        by register()'s own supersede semantics), not a new one, so it
        must never be blocked here. Counts live registry occupancy (via
        _identity_by_key), not identity binding history — a disconnected
        node's old key doesn't count against the cap. See the design doc
        for issue #35."""
        if public_key in self._nodes:
            return
        current_count = sum(
            1
            for node in self._nodes.values()
            if (bound := self._identity_by_key.get(node.public_key)) is not None
            and bound.id == identity.id
        )
        if current_count >= self._per_identity_cap:
            raise IdentityCapReached(
                f"identity has reached the maximum of {self._per_identity_cap} registered nodes"
            )

    def ban_identity(self, login: str) -> list[Node]:
        """Ban the identity currently bound to GitHub login `login` and
        return the list of currently-registered Node objects under that
        identity, so the caller (server.py) can disconnect them. Does NOT
        unregister them itself — see the design doc for issue #37 on why
        that's server.py's job, reusing the existing superseded-connection
        cleanup path rather than duplicating it. Raises UnknownIdentity if
        the coordinator has never seen `login` bind to any public key.
        Resolves by scanning currently-known identity bindings rather than
        keeping a separate login index — bindings are already the sole
        source of truth for "what identities has this coordinator seen,"
        and there's no volume concern at this scale."""
        identity = next(
            (bound for bound in self._identity_by_key.values() if bound.login == login),
            None,
        )
        if identity is None:
            raise UnknownIdentity(f"no known identity bound to GitHub login {login!r}")
        self._banned_identity_ids.add(identity.id)
        return [
            node
            for node in self._nodes.values()
            if (bound := self._identity_by_key.get(node.public_key)) is not None
            and bound.id == identity.id
        ]

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

    def record_client_fault(self, public_key: str) -> None:
        self._reputation.setdefault(public_key, ReputationCounters()).client_faults += 1

    def _reputation_weight(self, public_key: str) -> float:
        """Laplace-smoothed success rate for public_key — 1.0 for a node
        with no recorded history (never penalizes the unproven), never
        exactly 0 no matter how many failures accumulate. See the design
        doc for issue #36. client_faults is deliberately excluded from
        `total`: it's a self-reported, unverifiable claim from an
        untrusted volunteer, and letting it move routing would recreate
        the exact bug issue #58 fixes."""
        counters = self._reputation.get(public_key)
        if counters is None:
            return 1.0
        total = counters.completions + counters.timeouts + counters.crashes + counters.disconnects
        return (counters.completions + 1) / (total + 1)

    def find_node_for_model(
        self, model: str, exclude: frozenset[str] = frozenset()
    ) -> Node | None:
        """Return a registered node hosting `model`, weighted by recent
        reliability, or None if none match — see the design doc for
        issue #11 (the original round-robin mechanism) and issue #36
        (the weighting pass added on top of it).

        Rotation state is the public_key this returned last time for
        `model`; if every current candidate has an identical reputation
        weight (true whenever none of them have recorded history yet —
        every case the original round-robin behavior was built and
        tested against), the next call returns the candidate after that
        one, wrapping around, exactly as before. If that node has since
        left the registry (or is itself excluded), rotation restarts
        from the front of the current candidate list — no fairness
        guarantee across registry churn, only "don't always pick the
        same node when several are equally healthy."

        When weights genuinely differ, a weighted-random draw (via the
        injected random_source) replaces that deterministic step — still
        capable of picking a less-reliable node, just less often. Either
        way, `_last_returned` is updated to whichever node was actually
        picked, so the rotation continues meaningfully from wherever a
        weighted draw landed.

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

        weights = [self._reputation_weight(node.public_key) for node in candidates]

        if len(set(weights)) == 1:
            start = 0
            last = self._last_returned.get(model)
            if last is not None:
                keys = [node.public_key for node in candidates]
                if last in keys:
                    start = (keys.index(last) + 1) % len(candidates)
            node = candidates[start]
        else:
            node = self._random.choices(candidates, weights=weights, k=1)[0]

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

    def list_models(self) -> list[dict]:
        """The distinct models currently served, with how many registered
        nodes serve each — the client-facing view of the registry.

        Deliberately built up from model strings and counts rather than
        derived by stripping fields from list_nodes(): a client token must
        not be able to enumerate volunteer identities, and a subtractive
        view would expose every future operator-facing field by default
        until someone remembered to strip it. See the design doc for
        issue #56.

        Sorted by model string so the response is deterministic for a
        given registry state — registry iteration is insertion order,
        which would otherwise shuffle as volunteers come and go.

        "Healthy" means registered: a node that stops answering the
        keepalive is already evicted (see the design doc for issue #9),
        so registration is liveness. Advisory only — a node can
        disconnect between a client's check and its call.
        """
        counts: dict[str, int] = {}
        for node in self._nodes.values():
            counts[node.model] = counts.get(node.model, 0) + 1
        return [
            {"model": model, "healthy_nodes": counts[model]}
            for model in sorted(counts)
        ]

    def handles_for(self, public_key: str) -> dict:
        """Opaque per-coordinator-process handles for the node at
        `public_key` and for the GitHub identity bound to it — what a
        client is told about who served its hop.

        A client can group hops by these ("did one volunteer read three
        of my five hops?") but cannot resolve them to a public key,
        fingerprint or GitHub login. That distinction is the whole point:
        ADR-0004's exposure claim has to be checkable by the client
        without handing clients the volunteer identities issue #56
        deliberately withholds.

        identity_handle is None if public_key has no bound identity.
        Registration always binds one, so that shouldn't happen — but a
        completion must not fail because a reporting field couldn't be
        built, and None here means "unexpectedly unbound", never
        "anonymous node".
        """
        identity = self._identity_by_key.get(public_key)
        return {
            "node_handle": crypto.handle(self._handle_secret, public_key),
            "identity_handle": (
                crypto.handle(self._handle_secret, identity.id) if identity is not None else None
            ),
        }
