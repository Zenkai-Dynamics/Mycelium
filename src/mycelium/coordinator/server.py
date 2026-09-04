"""WebSocket server the coordinator runs to accept dial-out node connections.

See ADR-0002 for why nodes dial out rather than the coordinator dialing in.
Handles the registration handshake and registry status queries — see the
design doc for issue #8. Node liveness is tracked via the WebSocket
ping/pong keepalive (PING_INTERVAL_SECONDS/PING_TIMEOUT_SECONDS/
CLOSE_TIMEOUT_SECONDS below) — see the design doc for issue #9: a node
that goes silent gets its connection closed by the `websockets` library
itself, which `_handle_registration`'s `finally: registry.unregister(...)`
already turns into a registry drop, the same as any other disconnect.
Routing a client request to a healthy node (#10) is handled by the
`"complete"` branch below and mycelium.coordinator.router.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import websockets

from mycelium import crypto
from mycelium.coordinator import completion_request, github_identity, router
from mycelium.coordinator.registry import (
    IdentityBanned,
    IdentityCapReached,
    MissingGithubToken,
    Node,
    NodeRegistry,
    UnknownIdentity,
)

# These three also double as #9's node-liveness mechanism: a silent node
# (no pong within PING_TIMEOUT_SECONDS of a ping) has the library start a
# close handshake it can never complete, so its connection is force-closed
# after CLOSE_TIMEOUT_SECONDS more — which _handle_registration's cleanup
# already turns into a registry drop. See
# test_silently_unresponsive_node_is_dropped_within_ping_timeout_window in
# tests/coordinator/test_server.py. Worst case from "node goes silent" to
# "dropped from the registry": PING_INTERVAL_SECONDS + PING_TIMEOUT_SECONDS
# + CLOSE_TIMEOUT_SECONDS ≈ 50s.
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20
CLOSE_TIMEOUT_SECONDS = 10
# Bumped from 10.0 (issue #39): registration can now involve a GitHub API
# call (NodeRegistry.resolve_identity), bounded at
# github_identity.VERIFY_TIMEOUT_SECONDS (5s). Kept numerically equal to
# registration.REGISTRATION_TIMEOUT_SECONDS by convention (see
# test_server_and_registration_agree_on_timeout_settings in
# tests/test_integration.py) — this constant's own job (bounding time to
# receive the first message at all) doesn't itself depend on GitHub.
FIRST_MESSAGE_TIMEOUT_SECONDS = 15.0

# Fire-and-forget cleanup tasks (closing a superseded connection) must keep
# a reference somewhere, or asyncio may garbage-collect them mid-execution.
# This module-level set is that reference; each task removes itself when done.
_background_tasks: set[asyncio.Task] = set()


def _close_in_background(websocket) -> None:
    task = asyncio.create_task(websocket.close())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _handle_node(websocket, registry: NodeRegistry) -> None:
    """Read the first message and dispatch on it: a node registration, a
    status query, or a client's completion request. A registered node's
    connection is then held open for routed requests (see
    _handle_registration below); a status query or completion request
    gets one response and the connection closes. Anything else — no
    message within the timeout, malformed JSON, an unrecognized type —
    closes the connection."""
    try:
        # asyncio.timeout(), not asyncio.wait_for() — see registration.py's
        # matching comment: wait_for has a Python 3.11 cancellation race
        # this side is equally exposed to.
        async with asyncio.timeout(FIRST_MESSAGE_TIMEOUT_SECONDS):
            raw = await websocket.recv()
    except (TimeoutError, websockets.exceptions.ConnectionClosed):
        return

    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.close()
        return

    if not isinstance(message, dict):
        await websocket.close()
        return

    message_type = message.get("type")

    if message_type == "status_query":
        await _handle_status_query(websocket, registry, message)
        return

    if message_type == "register":
        await _handle_registration(websocket, registry, message)
        return

    if message_type == "ban_identity":
        await _handle_ban_request(websocket, registry, message)
        return

    if message_type == "complete":
        await _handle_complete_request(websocket, registry, message)
        return

    await websocket.close()


async def _handle_complete_request(websocket, registry: NodeRegistry, message: dict) -> None:
    """A client's one-shot completion request: authenticate, pick a
    healthy node hosting the requested model, forward, relay the result
    (or a clear error) back, then close — see the design doc for issue
    #10. If the picked node turns out to be disconnected, self-heal the
    registry and retry a different healthy node before giving up — see
    the design doc for issue #11. A timeout or a node-reported failure is
    not retried: the node might still be working, and silently re-running
    the same prompt on a second node risks double-executing it. Each
    outcome (completion/timeout/crash/disconnect) is recorded against the
    node that produced it — see the design doc for issue #36.

    Every reply also reports which nodes actually received the request
    (`exposed`, a list of opaque per-node/per-identity handles — never a
    public key, fingerprint, or GitHub login) and how long the whole
    retry loop took (`elapsed_ms`, present only once at least one node
    was attempted). A node is exposed the moment the request bytes leave
    the coordinator for it, whether or not it ever replies: a node that
    received the request and then timed out, dropped, or reported a
    failure read the content either way, but a node whose send never
    landed did not. See the design doc for issue #63."""
    if not registry.check_token(message.get("token")):
        await websocket.close()
        return

    exposed: list[dict] = []
    attempts = 0
    started = time.monotonic()

    async def reject(reason: str) -> None:
        reply = {"type": "complete_error", "reason": reason, "exposed": exposed}
        if attempts:
            reply["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        try:
            await websocket.send(json.dumps(reply))
        except websockets.exceptions.ConnectionClosed:
            return
        await websocket.close()

    model = message.get("model")
    if not model:
        await reject("model is required")
        return

    # The coordinator is the single place the one-message `prompt`
    # shorthand is expanded, so the node wire has exactly one shape —
    # see the design doc for issue #55.
    try:
        messages = completion_request.normalize_messages(message)
    except completion_request.InvalidCompletionRequest as exc:
        await reject(str(exc))
        return

    tried: set[str] = set()
    while True:
        try:
            node = registry.find_node_for_model(model, exclude=frozenset(tried))
            if node is None:
                raise router.NoHealthyNodeError(f"no healthy node for model {model!r}")
            attempts += 1
            # Passed explicitly (not relying on route_request's own default)
            # so tests can monkeypatch router.NODE_COMPLETE_TIMEOUT_SECONDS
            # and have it actually take effect: Python binds a default
            # argument value once at function-definition time, so the
            # default alone would never see a post-import monkeypatch.
            # Production behavior is unchanged — the constant is never
            # mutated after import there.
            text = await router.route_request(
                node, messages, timeout=router.NODE_COMPLETE_TIMEOUT_SECONDS
            )
        except router.NodeSendFailedError:
            # Nothing reached this node — it saw none of the content, so
            # it is deliberately NOT added to `exposed`. route_request
            # raises this only when the connection was demonstrably not
            # OPEN before the send, which is the one case where websockets
            # is guaranteed to have written no bytes; a send that failed
            # part-way through arrives as NodeDroppedError below and does
            # count as exposure. See the design doc for issue #63.
            registry.record_disconnect(node.public_key)
            registry.unregister(node.public_key, node.websocket)
            tried.add(node.public_key)
            continue
        except router.NodeDisconnectedError:
            # NodeDroppedError: the node may already have received the
            # request — it died awaiting a reply, or the send failed on a
            # connection that was still OPEN and so may have hit the wire.
            # Reported as exposure even though it never answered: an
            # ambiguous case counts as exposure. See the design doc for
            # issue #63.
            exposed.append(registry.handles_for(node.public_key))
            registry.record_disconnect(node.public_key)
            registry.unregister(node.public_key, node.websocket)
            tried.add(node.public_key)
            continue
        except router.NodeTimeoutError as exc:
            exposed.append(registry.handles_for(node.public_key))
            registry.record_timeout(node.public_key)
            await reject(str(exc))
            return
        except router.NodeError as exc:
            exposed.append(registry.handles_for(node.public_key))
            registry.record_crash(node.public_key)
            await reject(str(exc))
            return
        except router.RoutingError as exc:
            await reject(str(exc))
            return
        else:
            exposed.append(registry.handles_for(node.public_key))
            registry.record_completion(node.public_key)
            break

    reply = {
        "type": "complete_result",
        "text": text,
        "exposed": exposed,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    try:
        await websocket.send(json.dumps(reply))
    except websockets.exceptions.ConnectionClosed:
        return
    await websocket.close()


def _dispatch_node_message(node: Node, raw: str) -> None:
    """Resolve the pending future for a complete_result/complete_error
    reply from a registered node. Anything else — malformed JSON, an
    unrecognized type, a request_id with no matching pending future (e.g.
    a very late reply after route_request already gave up) — is silently
    ignored: this connection has no other job than routed request/response
    after registration, and there's no client left to usefully report a
    problem to."""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return
    if message.get("type") not in ("complete_result", "complete_error"):
        return
    future = node.pending.get(message.get("request_id"))
    if future is not None and not future.done():
        future.set_result(message)


async def _handle_status_query(websocket, registry: NodeRegistry, message: dict) -> None:
    if not registry.check_token(message.get("token")):
        await websocket.close()
        return
    await websocket.send(json.dumps({"type": "status", "nodes": registry.list_nodes()}))
    await websocket.close()


async def _handle_ban_request(websocket, registry: NodeRegistry, message: dict) -> None:
    """An operator's `mycelium-coordinator-ban` command: authenticate,
    ban the named GitHub login, then disconnect any currently-registered
    nodes under that identity — see the design doc for issue #37.
    Disconnecting reuses the same _close_in_background(...) path
    _handle_registration already uses for a superseded connection; each
    disconnected node's own long-lived _handle_registration task (still
    running for that node's original connection) notices
    ConnectionClosed and runs its existing finally-block cleanup
    (registry.unregister + failing any pending routed requests)."""
    if not registry.check_token(message.get("token")):
        await websocket.close()
        return

    identity = message.get("identity")
    if not identity:
        await websocket.send(json.dumps(
            {"type": "ban_failed", "reason": "identity is required"}
        ))
        await websocket.close()
        return

    try:
        disconnected_nodes = registry.ban_identity(identity)
    except UnknownIdentity as exc:
        await websocket.send(json.dumps({"type": "ban_failed", "reason": str(exc)}))
        await websocket.close()
        return

    for node in disconnected_nodes:
        _close_in_background(node.websocket)

    await websocket.send(json.dumps({
        "type": "banned", "identity": identity, "disconnected_count": len(disconnected_nodes),
    }))
    await websocket.close()


async def _handle_registration(websocket, registry: NodeRegistry, message: dict) -> None:
    node_id = message.get("node_id")
    model = message.get("model")
    if not node_id or not model:
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "node_id and model are required"}
        ))
        await websocket.close()
        return

    public_key = message.get("public_key")
    signature = message.get("signature")
    if not public_key or not signature:
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "public_key and signature are required"}
        ))
        await websocket.close()
        return

    if not crypto.verify_registration_signature(public_key, signature):
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "invalid signature"}
        ))
        await websocket.close()
        return

    # Collapse non-canonical base64 spellings of the same raw key to one
    # string, so the registry's string-keyed dicts can't be handed the
    # same key twice under different spellings — see
    # crypto.canonical_public_key.
    public_key = crypto.canonical_public_key(public_key)

    try:
        identity = await registry.resolve_identity(public_key, message.get("github_token"))
    except MissingGithubToken:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": "github_token is required for first-time registration",
        }))
        await websocket.close()
        return
    except github_identity.InvalidGithubToken:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": "invalid or expired GitHub token",
        }))
        await websocket.close()
        return
    except github_identity.IdentityVerificationError:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": "could not reach GitHub to verify identity, try again",
        }))
        await websocket.close()
        return

    try:
        registry.enforce_not_banned(identity)
    except IdentityBanned as exc:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": str(exc),
        }))
        await websocket.close()
        return

    try:
        registry.enforce_identity_cap(public_key, identity)
    except IdentityCapReached as exc:
        await websocket.send(json.dumps({
            "type": "registration_rejected",
            "reason": str(exc),
        }))
        await websocket.close()
        return

    superseded = registry.register(public_key, node_id, model, websocket)
    # Captured once, right now — never re-fetched from the registry later.
    # If this node reconnects again before this connection's cleanup runs,
    # a fresh registry.get(public_key) at that point would return the
    # *newer* connection's Node, not this one. See the design doc for
    # issue #10.
    node = registry.get(public_key)
    # Ack the new connection FIRST — closing a superseded connection can
    # block for its full close_timeout if that connection is a half-dead
    # zombie (the common case: a node reconnecting after a network blip,
    # before the old socket's own ping/pong has noticed it's gone). The
    # new node's registration must not wait behind that cleanup.
    await websocket.send(json.dumps({"type": "registered"}))
    if superseded is not None:
        _close_in_background(superseded.websocket)

    try:
        async for raw in websocket:
            _dispatch_node_message(node, raw)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        registry.unregister(public_key, websocket)
        # Anything still waiting on this connection (issue #10) needs to
        # fail now, not sit out the full route_request timeout for a node
        # that's already visibly gone — whether cleanly closed, silently
        # timed out via ping/pong (#9), or superseded by a reconnect
        # (_close_in_background above, which triggers this same cleanup
        # for the *old* connection's own _handle_registration task).
        #
        # NodeDroppedError specifically, not the NodeDisconnectedError
        # base: reaching here means the request was already sent and this
        # node had it in hand when the connection died, so it counts as
        # exposure. The class choice is what puts the node in the reply's
        # `exposed` list — see the design doc for issue #63.
        for pending_future in node.pending.values():
            if not pending_future.done():
                pending_future.set_exception(
                    router.NodeDroppedError("node disconnected mid-request")
                )


def build_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Build the server-side TLS context from the coordinator's cert/key pair."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def serve(
    host: str,
    port: int,
    cert_path: Path,
    key_path: Path,
    token: str,
    identity_verifier: Callable[[str], Awaitable] | None = None,
    per_identity_cap: int | None = None,
    handle_secret: bytes | None = None,
):
    """Start the coordinator's node-facing WebSocket server.

    identity_verifier overrides the real GitHub identity check (see
    mycelium.coordinator.github_identity.verify_identity) — production
    callers leave it unset; tests inject a fake so no test ever makes a
    real network call to GitHub. See the design doc for issue #39.

    per_identity_cap overrides NodeRegistry's default Sybil-resistance
    cap (3) — production callers leave it unset unless the operator
    configured a different value via mycelium-coordinator's
    --per-identity-cap flag. See the design doc for issue #35. Resolved
    to the literal default here (not passed through as None) because
    NodeRegistry.__init__ takes an ordinary `int = 3` default, not a
    None-accepting parameter — `None >= 3` would raise inside
    enforce_identity_cap if passed through unconditionally.

    handle_secret overrides NodeRegistry's own randomly-generated
    per-process exposure-handle secret — production callers leave it
    unset; tests inject a fixed secret so the resulting handles are
    assertable. See the design doc for issue #63.

    Returns whatever `websockets.serve` returns: awaitable to get a `Server`
    instance directly, or usable as `async with serve(...) as server:`.
    """
    ssl_context = build_ssl_context(cert_path, key_path)
    registry = NodeRegistry(
        token,
        identity_verifier=identity_verifier,
        per_identity_cap=per_identity_cap if per_identity_cap is not None else 3,
        handle_secret=handle_secret,
    )

    async def handler(websocket):
        await _handle_node(websocket, registry)

    return websockets.serve(
        handler,
        host,
        port,
        ssl=ssl_context,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
        close_timeout=CLOSE_TIMEOUT_SECONDS,
    )
