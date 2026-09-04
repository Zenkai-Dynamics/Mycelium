"""Tests for mycelium.coordinator.server."""

import asyncio
import base64
import hashlib
import json
import ssl
import string
import time

import pytest
import websockets
from websockets.protocol import State

from mycelium import crypto
from mycelium.coordinator import certs, router, server
from mycelium.coordinator import github_identity
from mycelium.coordinator.registry import NodeRegistry

PUBKEY_A = base64.b64encode(b"a" * 32).decode()
PUBKEY_B = base64.b64encode(b"b" * 32).decode()

_B64_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "+/"


def _non_canonical_variant(public_key_b64_str: str) -> str:
    """Brute-force an alternate base64 spelling of the same raw bytes as
    public_key_b64_str — see the matching helper in tests/test_crypto.py
    for why this is possible (2 unused low bits in the second-to-last
    base64 character of a 32-byte payload)."""
    raw = base64.b64decode(public_key_b64_str)
    for candidate_char in _B64_ALPHABET:
        candidate = public_key_b64_str[:-2] + candidate_char + public_key_b64_str[-1]
        if candidate == public_key_b64_str:
            continue
        if base64.b64decode(candidate, validate=True) == raw:
            return candidate
    raise AssertionError(f"no non-canonical variant found for {public_key_b64_str!r}")


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


def _register_payload(
    node_id: str, model: str, github_token: str | None = "valid-github-token"
) -> tuple[dict, str]:
    """Build a valid register message with a freshly generated Ed25519
    keypair. Returns (payload, public_key_b64) so callers can compute the
    expected fingerprint for status-query assertions. github_token
    defaults to a value _fake_identity_verifier always accepts; pass None
    to omit the field entirely (e.g. a reconnect that needs none) or
    "bad-github-token" to test rejection. Reconnect tests that need the
    SAME key across two registrations build their payload dicts directly
    instead of through this helper — it exists to keep the many one-shot,
    fresh-key call sites for other tests short."""
    private_key = crypto.generate_keypair()
    public_key_b64 = crypto.public_key_b64(private_key)
    signature_b64 = crypto.sign_public_key(private_key)
    payload = {
        "type": "register",
        "model": model,
        "node_id": node_id,
        "public_key": public_key_b64,
        "signature": signature_b64,
    }
    if github_token is not None:
        payload["github_token"] = github_token
    return payload, public_key_b64


async def _fake_identity_verifier(github_token: str) -> github_identity.GithubIdentity:
    """Accepts any token except the sentinels "bad-github-token" (a real
    GitHub rejection) and "unreachable-github-token" (GitHub couldn't be
    reached at all) — see the design doc for issue #39. Injected into
    every server.serve(...) call in this file so no test ever makes a
    real network call to GitHub."""
    if github_token == "bad-github-token":
        raise github_identity.InvalidGithubToken("bad token")
    if github_token == "unreachable-github-token":
        raise github_identity.GithubUnreachable("could not reach GitHub: timed out")
    return github_identity.GithubIdentity(id="1", login="octocat")


async def test_node_can_connect_over_tls(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            assert ws.state.name == "OPEN"


async def test_multiple_nodes_can_connect_simultaneously(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws1:
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws2:
                assert ws1.state.name == "OPEN"
                assert ws2.state.name == "OPEN"


async def test_connection_with_wrong_pinned_cert_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    other_cert_path = tmp_path / "other-cert.pem"
    other_key_path = tmp_path / "other-key.pem"
    certs.ensure_cert(other_cert_path, other_key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        wrong_ctx = _client_ssl_context(other_cert_path)
        try:
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=wrong_ctx):
                assert False, "expected connection to be rejected"
        except ssl.SSLCertVerificationError:
            pass


async def test_server_survives_abnormal_disconnect(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        assert ws.state.name == "OPEN"
        ws.transport.close()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws2:
            assert ws2.state.name == "OPEN"


async def test_valid_registration_is_accepted(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            payload, public_key = _register_payload("node-a", "m")
            await ws.send(json.dumps(payload))
            response = json.loads(await ws.recv())
            assert response == {"type": "registered"}


async def test_registered_node_appears_in_status_query(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "Qwen/Qwen2.5-7B-Instruct")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()  # consume the "registered" ack

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response == {
                    "type": "status",
                    "nodes": [{
                        "node_id": "node-a",
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                        "fingerprint": crypto.fingerprint(public_key),
                        "identity": "octocat",
                        "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
                    }],
                }


async def test_disconnected_node_is_removed_from_registry(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, public_key = _register_payload("node-a", "m")
        await node_ws.send(json.dumps(payload))
        await node_ws.recv()
        await node_ws.close()
        await asyncio.sleep(0.2)  # let the coordinator notice the disconnect

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []


async def test_duplicate_public_key_replaces_and_closes_old_connection(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        old_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        private_key = crypto.generate_keypair()
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)
        await old_ws.send(json.dumps({
            "type": "register", "model": "model-a", "node_id": "node-a",
            "public_key": public_key, "signature": signature,
            "github_token": "valid-github-token",
        }))
        await old_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_ws:
            await new_ws.send(json.dumps({
                "type": "register", "model": "model-b", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
            }))
            await new_ws.recv()

            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await old_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"] == [
                    {
                        "node_id": "node-a",
                        "model": "model-b",
                        "fingerprint": crypto.fingerprint(public_key),
                        "identity": "octocat",
                        "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
                    }
                ]


async def test_reregistration_with_non_canonical_public_key_spelling_is_treated_as_same_node(tmp_path):
    """A non-canonical base64 spelling of the SAME raw public key (its real
    signature verifies regardless of which spelling wraps it — the
    signature is over the raw bytes) must not let one keypair occupy two
    registry entries. See Finding 1 of the final whole-branch review for
    issue #33."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        private_key = crypto.generate_keypair()
        canonical_public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)
        non_canonical_public_key = _non_canonical_variant(canonical_public_key)
        assert non_canonical_public_key != canonical_public_key

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws_first:
            await ws_first.send(json.dumps({
                "type": "register", "model": "model-a", "node_id": "node-a",
                "public_key": canonical_public_key, "signature": signature,
                "github_token": "valid-github-token",
            }))
            await ws_first.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws_second:
                await ws_second.send(json.dumps({
                    "type": "register", "model": "model-b",
                    "node_id": "node-a", "public_key": non_canonical_public_key, "signature": signature,
                }))
                await ws_second.recv()

                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                    await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                    response = json.loads(await status_ws.recv())
                    assert response["nodes"] == [
                        {
                            "node_id": "node-a",
                            "model": "model-b",
                            "fingerprint": crypto.fingerprint(canonical_public_key),
                            "identity": "octocat",
                            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
                        }
                    ]


async def test_different_public_keys_with_same_node_id_both_remain_registered(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws_a:
            payload_a, public_key_a = _register_payload("node-a", "model-a")
            await ws_a.send(json.dumps(payload_a))
            await ws_a.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws_b:
                # A stranger claiming the SAME node_id with a DIFFERENT
                # keypair must not supersede the first node — this is the
                # hijack issue #33 exists to close.
                payload_b, public_key_b = _register_payload("node-a", "model-a")
                await ws_b.send(json.dumps(payload_b))
                await ws_b.recv()

                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                    await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                    response = json.loads(await status_ws.recv())
                    fingerprints = {n["fingerprint"] for n in response["nodes"]}
                    assert len(response["nodes"]) == 2
                    assert fingerprints == {crypto.fingerprint(public_key_a), crypto.fingerprint(public_key_b)}


async def test_connection_with_no_message_is_closed_after_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "FIRST_MESSAGE_TIMEOUT_SECONDS", 0.3)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=2.0)


async def test_registration_with_non_dict_json_is_closed_not_crashed(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps([1, 2, 3]))  # valid JSON, not a dict
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()

        # Server must still be accepting new connections afterward.
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws2:
            await ws2.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await ws2.recv())
            assert response == {"type": "status", "nodes": []}


async def test_registration_missing_public_key_or_signature_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "register", "model": "m", "node_id": "node-a"}
            ))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "public_key and signature are required",
            }


async def test_registration_with_invalid_signature_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            key_a = crypto.generate_keypair()
            key_b = crypto.generate_keypair()
            await ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": crypto.public_key_b64(key_a),
                "signature": crypto.sign_public_key(key_b),  # signed by the WRONG key
            }))
            response = json.loads(await ws.recv())
            assert response == {"type": "registration_rejected", "reason": "invalid signature"}
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_registration_new_key_without_github_token_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            payload, _ = _register_payload("node-a", "m", github_token=None)
            await ws.send(json.dumps(payload))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "github_token is required for first-time registration",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_registration_new_key_with_invalid_github_token_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            payload, _ = _register_payload("node-a", "m", github_token="bad-github-token")
            await ws.send(json.dumps(payload))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "invalid or expired GitHub token",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_registration_new_key_with_github_unreachable_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            payload, _ = _register_payload("node-a", "m", github_token="unreachable-github-token")
            await ws.send(json.dumps(payload))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "could not reach GitHub to verify identity, try again",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_registration_reconnect_with_known_key_needs_no_github_token(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        private_key = crypto.generate_keypair()
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as first_ws:
            await first_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
                "github_token": "valid-github-token",
            }))
            response = json.loads(await first_ws.recv())
            assert response == {"type": "registered"}

        # Reconnect with the SAME key and NO github_token at all — must
        # still succeed, since the identity is already bound.
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
            await second_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
            }))
            response = json.loads(await second_ws.recv())
            assert response == {"type": "registered"}

async def test_registration_rejected_when_identity_cap_reached(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, per_identity_cap=1,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as first_ws:
            payload, _ = _register_payload("node-a", "m")
            await first_ws.send(json.dumps(payload))
            response = json.loads(await first_ws.recv())
            assert response == {"type": "registered"}

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
                # A DIFFERENT key, same fake identity ("octocat") — the
                # cap of 1 is already occupied by the first connection.
                payload, _ = _register_payload("node-b", "m")
                await second_ws.send(json.dumps(payload))
                response = json.loads(await second_ws.recv())
                assert response == {
                    "type": "registration_rejected",
                    "reason": "identity has reached the maximum of 1 registered nodes",
                }
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await second_ws.recv()


async def test_registration_succeeds_after_identity_cap_slot_freed_by_disconnect(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, per_identity_cap=1,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        first_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, _ = _register_payload("node-a", "m")
        await first_ws.send(json.dumps(payload))
        await first_ws.recv()

        await first_ws.close()
        await asyncio.sleep(0.2)  # let the coordinator notice the disconnect

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
            payload, _ = _register_payload("node-b", "m")
            await second_ws.send(json.dumps(payload))
            response = json.loads(await second_ws.recv())
            assert response == {"type": "registered"}


async def test_registration_reconnect_of_existing_key_not_blocked_by_identity_cap(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, per_identity_cap=1,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        private_key = crypto.generate_keypair()
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)

        old_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await old_ws.send(json.dumps({
            "type": "register", "model": "m", "node_id": "node-a",
            "public_key": public_key, "signature": signature,
            "github_token": "valid-github-token",
        }))
        await old_ws.recv()

        # Register AGAIN under the SAME key while old_ws is still open and
        # still occupying the (only) cap slot — the cap must not block
        # this, since it's the same key re-registering (a reconnect), not
        # a new one. If the exemption were broken, this would be rejected
        # with "identity has reached the maximum of 1 registered nodes"
        # instead of succeeding.
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_ws:
            await new_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
            }))
            response = json.loads(await new_ws.recv())
            assert response == {"type": "registered"}

        await old_ws.close()


async def test_duplicate_node_id_registration_acks_promptly_even_if_old_connection_is_unresponsive(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        old_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        private_key = crypto.generate_keypair()
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)
        await old_ws.send(json.dumps({
            "type": "register", "model": "model-a", "node_id": "node-a",
            "public_key": public_key, "signature": signature,
            "github_token": "valid-github-token",
        }))
        await old_ws.recv()
        # Simulate a zombie connection: stop reading, so it can't complete
        # a close handshake promptly when the coordinator later closes it.
        old_ws.transport.pause_reading()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_ws:
            await new_ws.send(json.dumps({
                "type": "register", "model": "model-b", "node_id": "node-a",
                "public_key": public_key, "signature": signature,
                "github_token": "valid-github-token",
            }))
            start = time.monotonic()
            response = json.loads(await asyncio.wait_for(new_ws.recv(), timeout=3.0))
            elapsed = time.monotonic() - start
            assert response == {"type": "registered"}
            assert elapsed < 2.0, (
                f"ack took {elapsed:.2f}s — must not block on closing a zombie superseded connection"
            )


async def test_silently_unresponsive_node_is_dropped_within_ping_timeout_window(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PING_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(server, "PING_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(server, "CLOSE_TIMEOUT_SECONDS", 0.2)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, public_key = _register_payload("node-a", "m")
        await node_ws.send(json.dumps(payload))
        await node_ws.recv()  # consume the "registered" ack

        # Confirm the node appears in the registry before the liveness window
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response == {
                "type": "status",
                "nodes": [{
                    "node_id": "node-a",
                    "model": "m",
                    "fingerprint": crypto.fingerprint(public_key),
                    "identity": "octocat",
                    "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
                }],
            }

        # Simulate the node going silent (network partition, frozen process):
        # stop processing incoming bytes, so it can never answer a ping with
        # a pong, and can never complete the close handshake the coordinator
        # starts after that — without ever sending a WebSocket close frame
        # itself. This is a different failure mode than
        # test_disconnected_node_is_removed_from_registry (clean close) or
        # test_server_survives_abnormal_disconnect (abrupt transport close)
        # — neither of those goes through the ping/pong timeout path this
        # test targets.
        node_ws.transport.pause_reading()

        # Worst case per the design doc for issue #9: PING_INTERVAL_SECONDS
        # to notice the silence, PING_TIMEOUT_SECONDS for the pong that
        # never arrives, then CLOSE_TIMEOUT_SECONDS waiting for a close
        # handshake the silent peer can never complete.
        await asyncio.sleep(
            server.PING_INTERVAL_SECONDS
            + server.PING_TIMEOUT_SECONDS
            + server.CLOSE_TIMEOUT_SECONDS
            + 1.0
        )

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []


async def _run_fake_node(node_ws, reply_for) -> None:
    """Stand-in for a real node agent: replies to every "complete" message
    it receives using reply_for(message) -> dict (the reply body, minus
    request_id — this helper fills that in)."""
    async for raw in node_ws:
        message = json.loads(raw)
        reply = reply_for(message)
        reply["request_id"] = message["request_id"]
        await node_ws.send(json.dumps(reply))


class _FakeNodeWebsocket:
    """Stand-in for a node's websocket, for testing
    server._handle_complete_request's failover logic in isolation from a
    real network connection — mirrors test_router.py's _FakeNodeWebsocket.

    `state` mirrors websockets' own Connection.state. route_request
    samples it before sending, so a fake that raises on send must also
    say whether it was already dead (State.CLOSED — a genuine failed
    send) or still OPEN (a mid-send failure, which counts as exposure).
    See the design doc for issue #63.
    """

    def __init__(self, send_raises=None, state=State.OPEN):
        self.sent: list[str] = []
        self._send_raises = send_raises
        self.state = state

    async def send(self, raw: str) -> None:
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append(raw)


class _FakeClientWebsocket:
    """Collects what _handle_complete_request sends back to the client,
    without a real network connection."""

    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        self.closed = True


async def test_complete_request_routes_to_registered_node_and_returns_result(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()  # consume "registered"
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_result", "text": f"echo: {msg['messages'][-1]['content']}"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hello"}
                ))
                response = json.loads(await client_ws.recv())
                assert response["type"] == "complete_result"
                assert response["text"] == "echo: hello"

            node_task.cancel()


async def test_complete_request_with_no_matching_node_returns_error(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "no-such-model", "prompt": "hi"}
            ))
            response = json.loads(await client_ws.recv())
            assert response["type"] == "complete_error"
            assert "no-such-model" in response["reason"]


async def test_complete_request_with_no_healthy_node_fails_fast_with_no_retry(tmp_path, monkeypatch):
    call_count = 0
    original_find_node_for_model = NodeRegistry.find_node_for_model

    def counting_find_node_for_model(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_find_node_for_model(self, *args, **kwargs)

    monkeypatch.setattr(NodeRegistry, "find_node_for_model", counting_find_node_for_model)

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "no-such-model", "prompt": "hi"}
            ))
            start = time.monotonic()
            response = json.loads(await client_ws.recv())
            elapsed = time.monotonic() - start

    assert response["type"] == "complete_error"
    assert "no-such-model" in response["reason"]
    assert elapsed < 1.0, (
        f"response took {elapsed:.2f}s — a missing-node error must be immediate, not a hang or a wait"
    )
    assert call_count == 1, (
        f"find_node_for_model was called {call_count} times — expected exactly 1 (no retry/poll loop)"
    )


async def test_complete_request_with_wrong_token_is_closed_without_reply(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "wrong", "model": "m", "prompt": "hi"}
            ))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await client_ws.recv()


async def test_complete_request_with_missing_prompt_returns_error(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m"}
            ))
            response = json.loads(await client_ws.recv())
            assert response["type"] == "complete_error"


async def test_complete_request_with_messages_reaches_the_node_intact(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "and of Spain?"},
    ]
    received: list[dict] = []

    def reply(msg):
        received.append(msg)
        return {"type": "complete_result", "text": "Madrid."}

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, _ = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()  # consume "registered"
            node_task = asyncio.create_task(_run_fake_node(node_ws, reply))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps({
                    "type": "complete", "token": "secret-token",
                    "model": "m", "messages": messages,
                }))
                response = json.loads(await client_ws.recv())

            node_task.cancel()

    assert response["type"] == "complete_result"
    assert response["text"] == "Madrid."
    assert received[0]["messages"] == messages, (
        "roles must survive coordinator -> node with the array unchanged"
    )


async def test_complete_request_with_both_prompt_and_messages_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps({
                "type": "complete", "token": "secret-token", "model": "m",
                "prompt": "hi", "messages": [{"role": "user", "content": "hi"}],
            }))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert "both" in response["reason"].lower()


async def test_complete_request_with_malformed_messages_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps({
                "type": "complete", "token": "secret-token", "model": "m",
                "messages": [{"role": "user"}],
            }))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert "content" in response["reason"]


async def test_complete_request_node_reports_failure_is_relayed_to_client(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_error", "reason": "vLLM exploded"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())
                assert response["type"] == "complete_error"
                assert response["reason"] == "vLLM exploded"

            node_task.cancel()


async def test_complete_request_success_increments_completion_counter(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_result", "text": f"echo: {msg['messages'][-1]['content']}"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                await client_ws.recv()

            node_task.cancel()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"][0]["reputation"] == {
                    "completions": 1, "timeouts": 0, "crashes": 0, "disconnects": 0,
                }


async def test_complete_request_node_failure_increments_crash_counter(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_error", "reason": "vLLM exploded"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                await client_ws.recv()

            node_task.cancel()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"][0]["reputation"]["crashes"] == 1


async def test_complete_request_node_disconnect_mid_request_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, public_key = _register_payload("node-a", "m")
        await node_ws.send(json.dumps(payload))
        await node_ws.recv()

        async def receive_then_never_reply():
            await node_ws.recv()  # accept the routed "complete", then go silent

        node_task = asyncio.create_task(receive_then_never_reply())

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
            ))
            await node_task  # make sure the node has received the routed request
            await node_ws.close()  # simulate the node dying mid-request

            start = time.monotonic()
            response = json.loads(await asyncio.wait_for(client_ws.recv(), timeout=5.0))
            elapsed = time.monotonic() - start
            assert response["type"] == "complete_error"
            assert elapsed < 3.0, (
                f"client waited {elapsed:.2f}s — a node disconnect mid-request must fail "
                "fast, not wait out the full 30s NODE_COMPLETE_TIMEOUT_SECONDS"
            )


async def test_complete_request_timeout_increments_timeout_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 0.2)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()

            async def receive_then_never_reply():
                await node_ws.recv()

            node_task = asyncio.create_task(receive_then_never_reply())

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                await client_ws.recv()

            await node_task

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"][0]["reputation"]["timeouts"] == 1


async def test_complete_request_client_disconnect_before_reply_does_not_crash_server(
    tmp_path, caplog
):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, public_key = _register_payload("node-a", "m")
        await node_ws.send(json.dumps(payload))
        await node_ws.recv()

        async def reply_after_client_gone():
            raw = await node_ws.recv()  # the routed "complete"
            message = json.loads(raw)
            await asyncio.sleep(0.2)  # give the client time to disconnect first
            await node_ws.send(json.dumps(
                {"type": "complete_result", "request_id": message["request_id"], "text": "too late"}
            ))

        node_task = asyncio.create_task(reply_after_client_gone())

        client_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await client_ws.send(json.dumps(
            {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
        ))
        await client_ws.close()  # disconnect before the node replies

        await node_task  # let the coordinator attempt (and fail) its send to the gone client

        # Server must still be accepting new connections afterward — the
        # send failing above must not have crashed the connection handler.
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == [
                {
                    "node_id": "node-a",
                    "model": "m",
                    "fingerprint": crypto.fingerprint(public_key),
                    "identity": "octocat",
                    "reputation": {"completions": 1, "timeouts": 0, "crashes": 0, "disconnects": 0},
                }
            ]

        await node_ws.close()

    # The connection handler must have swallowed the send failure quietly,
    # not let it escape and get logged as a "connection handler failed"
    # error by the websockets library (the un-fixed behavior).
    assert not any(record.levelno >= 40 for record in caplog.records), (
        "an error was logged — the ConnectionClosed from sending to the "
        "already-gone client must be caught, not left to propagate"
    )


async def test_superseded_node_connection_fails_only_its_own_pending_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        old_node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        private_key = crypto.generate_keypair()
        public_key = crypto.public_key_b64(private_key)
        signature = crypto.sign_public_key(private_key)
        await old_node_ws.send(json.dumps({
            "type": "register", "model": "m",
            "node_id": "node-a", "public_key": public_key, "signature": signature,
            "github_token": "valid-github-token",
        }))
        await old_node_ws.recv()

        old_client_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)

        async def old_client_request():
            await old_client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "old"}
            ))
            return json.loads(await old_client_ws.recv())

        old_request_task = asyncio.create_task(old_client_request())

        # Confirm the routed "complete" actually reached the OLD connection
        # before proceeding, so we know route_request has registered a
        # pending future on the OLD node's captured Node object.
        routed = json.loads(await old_node_ws.recv())
        assert routed["type"] == "complete"

        # Register a second connection under the same node_id — supersedes
        # the old one (issue #8 behavior); the coordinator closes
        # old_node_ws in the background.
        new_node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        await new_node_ws.send(json.dumps({
            "type": "register", "model": "m",
            "node_id": "node-a", "public_key": public_key, "signature": signature,
            "github_token": "valid-github-token",
        }))
        await new_node_ws.recv()

        # The OLD client's pending request must fail fast via the OLD
        # connection's own disconnect cleanup — not sit out the full 30s
        # NODE_COMPLETE_TIMEOUT_SECONDS.
        start = time.monotonic()
        old_response = await asyncio.wait_for(old_request_task, timeout=5.0)
        elapsed = time.monotonic() - start
        assert old_response["type"] == "complete_error"
        assert elapsed < 3.0, (
            f"old client waited {elapsed:.2f}s — a superseded connection's cleanup must "
            "fail its own pending requests fast, not wait out the full 30s timeout"
        )

        # The NEW connection's own pending dict must be untouched by the
        # old connection's cleanup: route a fresh request through it and
        # confirm it gets a correct, normal reply.
        node_task = asyncio.create_task(_run_fake_node(
            new_node_ws, lambda msg: {"type": "complete_result", "text": f"echo: {msg['messages'][-1]['content']}"}
        ))
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as new_client_ws:
            await new_client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "new"}
            ))
            new_response = json.loads(await new_client_ws.recv())
            assert new_response["type"] == "complete_result"
            assert new_response["text"] == "echo: new"

        node_task.cancel()
        await old_client_ws.close()


async def test_complete_request_disconnect_increments_disconnect_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, public_key = _register_payload("node-a", "m")
        await node_ws.send(json.dumps(payload))
        await node_ws.recv()

        async def receive_then_never_reply():
            await node_ws.recv()

        node_task = asyncio.create_task(receive_then_never_reply())

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
            ))
            await node_task  # the node has received the routed request
            await node_ws.close()  # simulate the node dying mid-request
            await client_ws.recv()

        # The disconnected connection's Node entry is gone from _nodes
        # (issue #11's self-heal), so list_nodes() won't show it directly.
        # Confirm the counter survived by re-registering the SAME key —
        # if record_disconnect ran, the count carries into this fresh
        # connection (Task 1's persistence-across-reconnect property).
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as reconnect_ws:
            await reconnect_ws.send(json.dumps({
                "type": "register", "model": "m", "node_id": "node-a",
                "public_key": payload["public_key"], "signature": payload["signature"],
            }))
            await reconnect_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
                await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
                response = json.loads(await status_ws.recv())
                assert response["nodes"][0]["reputation"]["disconnects"] == 1


async def test_concurrent_complete_requests_to_same_node_get_correct_replies(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()

            async def flaky_reversing_node():
                # Reply out of arrival order, to prove correlation (not
                # send order) determines which client gets which answer.
                first = json.loads(await node_ws.recv())
                second = json.loads(await node_ws.recv())
                await node_ws.send(json.dumps({
                    "type": "complete_result", "request_id": second["request_id"],
                    "text": f"reply to: {second['messages'][-1]['content']}",
                }))
                await node_ws.send(json.dumps({
                    "type": "complete_result", "request_id": first["request_id"],
                    "text": f"reply to: {first['messages'][-1]['content']}",
                }))

            node_task = asyncio.create_task(flaky_reversing_node())

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_a:
                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_b:
                    await client_a.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "A"}
                    ))
                    await client_b.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "B"}
                    ))
                    response_a = json.loads(await client_a.recv())
                    response_b = json.loads(await client_b.recv())

            await node_task
            assert response_a["type"] == "complete_result"
            assert response_a["text"] == "reply to: A"
            assert response_b["type"] == "complete_result"
            assert response_b["text"] == "reply to: B"


async def test_complete_request_round_robins_across_two_healthy_nodes(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_a_ws:
            payload_a, _ = _register_payload("node-a", "m")
            await node_a_ws.send(json.dumps(payload_a))
            await node_a_ws.recv()
            node_a_task = asyncio.create_task(_run_fake_node(
                node_a_ws, lambda msg: {"type": "complete_result", "text": f"node-a: {msg['messages'][-1]['content']}"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b_ws:
                payload_b, _ = _register_payload("node-b", "m")
                await node_b_ws.send(json.dumps(payload_b))
                await node_b_ws.recv()
                node_b_task = asyncio.create_task(_run_fake_node(
                    node_b_ws, lambda msg: {"type": "complete_result", "text": f"node-b: {msg['messages'][-1]['content']}"}
                ))

                # Two separate connections, one per request: a client
                # connection is one-shot (the server closes it after
                # replying to a "complete" — see _handle_complete_request),
                # so a second request on the same connection would find it
                # already closed.
                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws_1:
                    await client_ws_1.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "1"}
                    ))
                    first = json.loads(await client_ws_1.recv())

                async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws_2:
                    await client_ws_2.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "2"}
                    ))
                    second = json.loads(await client_ws_2.recv())

                assert {first["text"], second["text"]} == {"node-a: 1", "node-b: 2"}

            node_a_task.cancel()
            node_b_task.cancel()


async def test_complete_request_fails_over_to_healthy_node_when_first_pick_is_dead():
    registry = NodeRegistry("secret-token")
    dead_ws = _FakeNodeWebsocket(
        send_raises=websockets.exceptions.ConnectionClosedError(None, None),
        state=State.CLOSED,
    )
    healthy_ws = _FakeNodeWebsocket()
    registry.register(PUBKEY_A, "node-a", "m", dead_ws)  # registers first -> round robin picks it first
    registry.register(PUBKEY_B, "node-b", "m", healthy_ws)

    async def reply_from_node_b():
        while not healthy_ws.sent:
            await asyncio.sleep(0.01)
        sent = json.loads(healthy_ws.sent[0])
        node_b = registry.get(PUBKEY_B)
        node_b.pending[sent["request_id"]].set_result(
            {"type": "complete_result", "text": "answer from node-b", "request_id": sent["request_id"]}
        )

    asyncio.create_task(reply_from_node_b())

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    sent = json.loads(client_ws.sent[0])
    assert sent["type"] == "complete_result"
    assert sent["text"] == "answer from node-b"
    # node-a's dead connection must have been self-healed out of the registry.
    assert registry.list_nodes() == [
        {
            "node_id": "node-b",
            "model": "m",
            "fingerprint": hashlib.sha256(b"b" * 32).hexdigest()[:12],
            "identity": None,
            "reputation": {"completions": 1, "timeouts": 0, "crashes": 0, "disconnects": 0},
        }
    ]


async def test_complete_request_does_not_fail_over_on_timeout(monkeypatch):
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 0.2)
    registry = NodeRegistry("secret-token")
    slow_ws = _FakeNodeWebsocket()  # accepts the send, never replies
    other_ws = _FakeNodeWebsocket()
    registry.register(PUBKEY_A, "node-a", "m", slow_ws)
    registry.register(PUBKEY_B, "node-b", "m", other_ws)

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    response = json.loads(client_ws.sent[0])
    assert response["type"] == "complete_error"
    assert other_ws.sent == []  # node-b was never contacted
    # A timeout isn't treated as a dead node — node-a stays registered.
    assert registry.list_nodes() == [
        {
            "node_id": "node-a", "model": "m",
            "fingerprint": hashlib.sha256(b"a" * 32).hexdigest()[:12],
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 1, "crashes": 0, "disconnects": 0},
        },
        {
            "node_id": "node-b", "model": "m",
            "fingerprint": hashlib.sha256(b"b" * 32).hexdigest()[:12],
            "identity": None,
            "reputation": {"completions": 0, "timeouts": 0, "crashes": 0, "disconnects": 0},
        },
    ]


async def test_complete_request_returns_error_when_every_node_is_dead():
    registry = NodeRegistry("secret-token")
    dead_a = _FakeNodeWebsocket(
        send_raises=websockets.exceptions.ConnectionClosedError(None, None), state=State.CLOSED
    )
    dead_b = _FakeNodeWebsocket(
        send_raises=websockets.exceptions.ConnectionClosedError(None, None), state=State.CLOSED
    )
    registry.register(PUBKEY_A, "node-a", "m", dead_a)
    registry.register(PUBKEY_B, "node-b", "m", dead_b)

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    response = json.loads(client_ws.sent[0])
    assert response["type"] == "complete_error"
    assert registry.list_nodes() == []


async def test_registration_rejected_when_identity_is_banned(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as first_ws:
            payload, _ = _register_payload("node-a", "m")
            await first_ws.send(json.dumps(payload))
            response = json.loads(await first_ws.recv())
            assert response == {"type": "registered"}

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ban_ws:
            await ban_ws.send(json.dumps(
                {"type": "ban_identity", "token": "secret-token", "identity": "octocat"}
            ))
            await ban_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
            payload, _ = _register_payload("node-b", "m")
            await second_ws.send(json.dumps(payload))
            response = json.loads(await second_ws.recv())
            assert response == {
                "type": "registration_rejected",
                "reason": "this identity has been banned by the operator",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await second_ws.recv()


async def test_ban_disconnects_currently_registered_node(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        node_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload, _ = _register_payload("node-a", "m")
        await node_ws.send(json.dumps(payload))
        await node_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ban_ws:
            await ban_ws.send(json.dumps(
                {"type": "ban_identity", "token": "secret-token", "identity": "octocat"}
            ))
            response = json.loads(await ban_ws.recv())
            assert response == {
                "type": "banned", "identity": "octocat", "disconnected_count": 1,
            }

        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await node_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as status_ws:
            await status_ws.send(json.dumps({"type": "status_query", "token": "secret-token"}))
            response = json.loads(await status_ws.recv())
            assert response["nodes"] == []


async def test_ban_request_with_wrong_token_is_closed_without_reply(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "ban_identity", "token": "wrong", "identity": "octocat"}
            ))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_ban_request_for_unknown_identity_returns_ban_failed(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token", identity_verifier=_fake_identity_verifier
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ws:
            await ws.send(json.dumps(
                {"type": "ban_identity", "token": "secret-token", "identity": "nobody"}
            ))
            response = json.loads(await ws.recv())
            assert response == {
                "type": "ban_failed",
                "reason": "no known identity bound to GitHub login 'nobody'",
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


async def test_ban_is_checked_before_identity_cap(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, per_identity_cap=1,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as first_ws:
            payload, _ = _register_payload("node-a", "m")
            await first_ws.send(json.dumps(payload))
            await first_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as ban_ws:
                await ban_ws.send(json.dumps(
                    {"type": "ban_identity", "token": "secret-token", "identity": "octocat"}
                ))
                await ban_ws.recv()

            # first_ws is still open here, so the identity is simultaneously
            # AT its cap of 1 and banned — a NEW key for that same identity
            # must see the ban rejection, not the cap rejection. If ban
            # weren't checked first, this would instead see
            # "identity has reached the maximum of 1 registered nodes".
            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as second_ws:
                payload, _ = _register_payload("node-b", "m")
                await second_ws.send(json.dumps(payload))
                response = json.loads(await second_ws.recv())
                assert response == {
                    "type": "registration_rejected",
                    "reason": "this identity has been banned by the operator",
                }


async def test_complete_result_carries_exposure_handles_and_timing(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_result", "text": "ok"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())

            node_task.cancel()

    assert len(response["exposed"]) == 1
    assert response["exposed"][0]["node_handle"] == crypto.handle(b"s" * 32, public_key)
    assert response["exposed"][0]["identity_handle"]
    assert isinstance(response["elapsed_ms"], int)
    # The reply must not carry anything that resolves a volunteer.
    assert "public_key" not in response
    assert "fingerprint" not in response
    assert "identity" not in response
    assert public_key not in json.dumps(response)
    assert crypto.fingerprint(public_key) not in json.dumps(response)


async def test_no_healthy_node_reports_empty_exposure_and_no_timing(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps(
                {"type": "complete", "token": "secret-token", "model": "nope", "prompt": "hi"}
            ))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert response["exposed"] == []
    assert "elapsed_ms" not in response, (
        "nothing was routed, so reporting a duration would assert routing took no time"
    )


async def test_malformed_request_reports_empty_exposure(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
            await client_ws.send(json.dumps({
                "type": "complete", "token": "secret-token", "model": "m",
                "messages": [{"role": "user"}],
            }))
            response = json.loads(await client_ws.recv())

    assert response["type"] == "complete_error"
    assert response["exposed"] == []


async def test_node_reported_failure_still_reports_that_node_as_exposed(tmp_path):
    """A node that read the request and then failed still read it."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_ws:
            payload, public_key = _register_payload("node-a", "m")
            await node_ws.send(json.dumps(payload))
            await node_ws.recv()
            node_task = asyncio.create_task(_run_fake_node(
                node_ws, lambda msg: {"type": "complete_error", "reason": "vLLM exploded"}
            ))

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())

            node_task.cancel()

    assert response["type"] == "complete_error"
    assert response["exposed"][0]["node_handle"] == crypto.handle(b"s" * 32, public_key)
    assert isinstance(response["elapsed_ms"], int)


async def test_failover_reports_both_the_dropped_node_and_the_one_that_answered(
    tmp_path, monkeypatch
):
    """The case a single-handle report would silently under-count: node A
    received the request and then dropped, node B answered. Both read it.

    The mid-flight-drop shape (receive the routed request, then close
    without replying) is the same one
    test_complete_request_disconnect_increments_disconnect_counter uses.
    Node A is picked first deterministically: with no reputation history
    every candidate weighs the same, so find_node_for_model falls back to
    registration order.
    """
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 30.0)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        node_a_ws = await websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx)
        payload_a, public_key_a = _register_payload("node-a", "m")
        await node_a_ws.send(json.dumps(payload_a))
        await node_a_ws.recv()

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b_ws:
            payload_b, public_key_b = _register_payload("node-b", "m")
            await node_b_ws.send(json.dumps(payload_b))
            await node_b_ws.recv()

            async def a_receives_then_dies():
                await node_a_ws.recv()
                await node_a_ws.close()

            async def b_answers():
                routed = json.loads(await node_b_ws.recv())
                await node_b_ws.send(json.dumps({
                    "type": "complete_result",
                    "request_id": routed["request_id"],
                    "text": "ok",
                }))

            a_task = asyncio.create_task(a_receives_then_dies())
            b_task = asyncio.create_task(b_answers())

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as client_ws:
                await client_ws.send(json.dumps(
                    {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                ))
                response = json.loads(await client_ws.recv())

            await a_task
            await b_task

    assert response["type"] == "complete_result"
    assert [entry["node_handle"] for entry in response["exposed"]] == [
        crypto.handle(b"s" * 32, public_key_a),
        crypto.handle(b"s" * 32, public_key_b),
    ], "the node that received-then-dropped read the request too, and must be reported"


async def test_a_timed_out_node_is_reported_as_exposed(monkeypatch):
    """The design's headline case, and the one that most needs asserting:
    a node that received the request and never answered is
    indistinguishable from one that read it and then failed, so the
    report must assume the volunteer saw it. See the design doc for
    issue #63."""
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 0.2)
    registry = NodeRegistry("secret-token", handle_secret=b"s" * 32)
    slow_ws = _FakeNodeWebsocket()  # accepts the send, never replies
    registry.register(PUBKEY_A, "node-a", "m", slow_ws)

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    response = json.loads(client_ws.sent[0])
    assert response["type"] == "complete_error"
    assert [entry["node_handle"] for entry in response["exposed"]] == [
        crypto.handle(b"s" * 32, PUBKEY_A)
    ], "the node read the request and may still be processing it"
    assert isinstance(response["elapsed_ms"], int)


async def test_client_facing_error_reason_never_names_the_node(monkeypatch):
    """node_id defaults to socket.gethostname() (node/cli.py), and a
    timeout reply carries exactly one exposed handle — so naming the node
    in `reason` would hand the client the handle -> machine-name mapping
    the whole exposure-handle feature exists to withhold. See the design
    doc for issue #63."""
    monkeypatch.setattr(router, "NODE_COMPLETE_TIMEOUT_SECONDS", 0.2)
    registry = NodeRegistry("secret-token", handle_secret=b"s" * 32)
    registry.register(PUBKEY_A, "volunteer-laptop.local", "m", _FakeNodeWebsocket())

    client_ws = _FakeClientWebsocket()
    await server._handle_complete_request(
        client_ws, registry, {"token": "secret-token", "model": "m", "prompt": "hi"}
    )

    response = json.loads(client_ws.sent[0])
    assert response["type"] == "complete_error"
    assert "volunteer-laptop.local" not in json.dumps(response)
    # Still useful: the client has to be able to tell a timeout from
    # anything else that could have gone wrong.
    assert "did not respond" in response["reason"]


async def test_a_node_whose_send_failed_is_not_reported_as_exposed(tmp_path, monkeypatch):
    """NodeSendFailedError means the bytes never left the coordinator, so
    that node saw nothing and must not appear in the report.

    route_request is faked rather than provoked: a real send-failure
    needs a node whose socket is dead but which is still registered, and
    the server's own disconnect cleanup races to unregister it — so the
    genuine article cannot be staged deterministically end to end. The
    router-level tests in Task 3 cover raising it for real; this covers
    the coordinator's handling of it.
    """
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    routed_to: list[str] = []

    async def send_fails_on_first_node(node, messages, timeout=None):
        routed_to.append(node.public_key)
        if len(routed_to) == 1:
            raise router.NodeSendFailedError(f"node {node.node_id!r} disconnected")
        return "ok"

    monkeypatch.setattr(router, "route_request", send_fails_on_first_node)

    async with server.serve(
        "127.0.0.1", 0, cert_path, key_path, "secret-token",
        identity_verifier=_fake_identity_verifier, handle_secret=b"s" * 32,
    ) as coordinator:
        port = coordinator.sockets[0].getsockname()[1]
        client_ctx = _client_ssl_context(cert_path)

        async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_a_ws:
            payload_a, public_key_a = _register_payload("node-a", "m")
            await node_a_ws.send(json.dumps(payload_a))
            await node_a_ws.recv()

            async with websockets.connect(f"wss://127.0.0.1:{port}", ssl=client_ctx) as node_b_ws:
                payload_b, public_key_b = _register_payload("node-b", "m")
                await node_b_ws.send(json.dumps(payload_b))
                await node_b_ws.recv()

                async with websockets.connect(
                    f"wss://127.0.0.1:{port}", ssl=client_ctx
                ) as client_ws:
                    await client_ws.send(json.dumps(
                        {"type": "complete", "token": "secret-token", "model": "m", "prompt": "hi"}
                    ))
                    response = json.loads(await client_ws.recv())

    assert routed_to == [public_key_a, public_key_b]
    assert [entry["node_handle"] for entry in response["exposed"]] == [
        crypto.handle(b"s" * 32, public_key_b)
    ], "node A's send never landed, so it read nothing and must not be reported"
