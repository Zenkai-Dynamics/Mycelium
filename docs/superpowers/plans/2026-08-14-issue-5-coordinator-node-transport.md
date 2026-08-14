# Issue #5 — Coordinator↔Node Transport — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A node process dials out to a coordinator process over TLS-secured WebSocket, holds the connection open (surviving idle periods), and automatically reconnects with exponential backoff if the connection drops — per the direction decided in [ADR-0002](../../adr/0002-node-transport-model.md).

**Architecture:** `websockets==17.0.1` for the transport (server on the coordinator, reconnecting client on the node), `cryptography==50.0.0` for a self-signed cert the coordinator generates and the node pins directly (no public CA — see the design doc for why). Every API shape below (handler signature, `serve`/`connect` kwargs, the `reconnect_delays` callable contract, the exact exception `ssl.SSLCertVerificationError` on a rejected cert, the `state.name == "OPEN"` check) was verified against the real installed library during design — this is not written from memory, and a real local smoke test and a real reconnect-after-server-restart test both passed before this plan was written.

**Tech Stack:** Python 3.10+, `websockets`, `cryptography`, stdlib `asyncio`/`ssl`/`argparse`. Test-first throughout (`pytest` + `pytest-asyncio`, both new to this repo — no test suite existed before this issue).

## Global Constraints

- Transport direction: the **node** dials out to the **coordinator**; the coordinator never initiates a connection to a node (ADR-0002). Do not build anything that has the coordinator opening an outbound connection to a node.
- Scope boundary: **nothing flows over the connection except WebSocket ping/pong keepalive frames.** No node ID, no auth token, no registration message, no coordinator-side registry of connected nodes. That's issue #8. If a task's tests seem to need any of that, stop and flag it — don't add it.
- No ASGI framework (FastAPI/Starlette). The coordinator is a bare `websockets` server. Issue #10 can introduce an HTTP framework later if it needs one.
- Keepalive: `ping_interval=20`, `ping_timeout=20` (seconds), on both the server and client sides — these are actually `websockets`' own defaults, but set them explicitly in code for documentation clarity rather than relying on the library default silently matching.
- Reconnect backoff (node side only — the coordinator doesn't reconnect to anything): initial delay 1.0s, ×2 multiplier each step, capped at 30.0s, ±20% jitter, retries indefinitely. Implement via `websockets.connect`'s built-in `reconnect_delays` parameter (a zero-arg callable returning a generator of floats) — do not hand-roll a separate reconnect loop; the library's `async for websocket in connect(...):` iterator already retries automatically using whatever generator `reconnect_delays` produces.
- TLS: self-signed cert, IP in the Subject Alternative Name, node pins the exact cert file via `ssl.SSLContext.load_verify_locations(cafile=...)` with `check_hostname = False` (pinning to an exact cert makes hostname matching redundant — this was verified to reject a wrong/different cert with `ssl.SSLCertVerificationError`, and to accept the correct one, in a real local test during design).
- New runtime dependencies: `websockets==17.0.1` as an **unconditional** project dependency (both `node` and `coordinator` need it — putting it in `[project.dependencies]` rather than duplicating the pin in both extras also means it installs without pulling in `ray`/`vllm`, which matters for fast local test iteration on non-Linux dev machines). `cryptography==50.0.0` added to the `coordinator` extra only (only the coordinator generates certs).
- New dev-only dependencies (test tooling, never shipped to users): `pytest==9.1.1`, `pytest-asyncio==1.4.0`, via a new `[dependency-groups] dev` entry in `pyproject.toml` (PEP 735 / uv-native — not `[project.optional-dependencies]`, which is for installable extras).
- `pyproject.toml`'s `[tool.uv].environments` restricts the whole project's **lockfile-based** resolution (`uv sync`, `uv run`) to Linux x86_64 (set in issue #2, confirmed to break `uv sync`/`uv run` on macOS during issue #3). This plan's local development/test commands use `uv venv` + `uv pip install -e .[...]` instead — the same workaround `docs/SETUP.md` already documents — which resolves directly against `pyproject.toml` and works on any OS. `websockets` and `cryptography` are pure/prebuilt-wheel packages with no Linux-only constraint of their own, so this works cleanly for every task in this plan except real hardware verification.
- Test layout: `tests/coordinator/` and `tests/node/`, each with an `__init__.py` (needed because both directories contain a `test_cli.py` — without `__init__.py`, pytest's default import mode can't disambiguate two same-named test files). `pyproject.toml` gets a `[tool.pytest.ini_options]` section with `testpaths = ["tests"]` and `asyncio_mode = "auto"` (so async test functions don't need individual `@pytest.mark.asyncio` decorators).
- After all coded tasks in this plan are complete and reviewed, a **real two-machine verification** happens — see the note after Task 6. That step is performed directly by the plan's controller (not dispatched to a fresh implementer subagent), because it requires coordinating live SSH sessions and credentials across three already-provisioned real machines (an Azure VM at `20.244.2.48`, and the `a6000`/`h100` SSH hosts) that a stateless subagent has no way to be handed safely. Do not skip it and do not attempt to delegate it.

---

### Task 1: Dependencies

**Files:**
- Modify: `pyproject.toml` (add `websockets` as an unconditional dependency, `cryptography` to the `coordinator` extra, a new `[dependency-groups]` section, a new `[tool.pytest.ini_options]` section)
- Modify: `uv.lock` (regenerate)
- Modify: `requirements-node-lock.txt` (regenerate, per the mechanism `docs/dependencies.md` already documents)
- Modify: `docs/dependencies.md` (record the new pins — it says "update in place as pins change")

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `websockets` and `cryptography` importable in a local dev environment, and `pytest`/`pytest-asyncio` available to run this plan's test suite. Every later task depends on this one.

- [ ] **Step 1: Edit `pyproject.toml`**

Add `websockets==17.0.1` as an unconditional dependency (new `[project.dependencies]` list — the file doesn't have one yet), add `cryptography==50.0.0` to the existing `coordinator` extra, and add a new `[dependency-groups]` section. The relevant parts of the file become:

```toml
[project]
name = "mycelium"
dynamic = ["version"]
description = "A framework for running LLM inference across GPUs volunteered by geographically separated users."
readme = "Readme.md"
requires-python = ">=3.10"
license = {file = "LICENSE"}
authors = [{name = "Varun Gambhir"}]
dependencies = [
    "websockets==17.0.1",
]
classifiers = [
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
]

[project.optional-dependencies]
node = [
    "ray[llm]==2.57.0",
    "vllm[audio]==0.25.1",
]
coordinator = [
    "cryptography==50.0.0",
]
client = []

[dependency-groups]
dev = [
    "pytest==9.1.1",
    "pytest-asyncio==1.4.0",
]
```

(Keep everything else in the file — `[build-system]`, `[project.scripts]`, `[tool.setuptools.*]`, `[tool.uv]` — exactly as it already is; only add the `dependencies` key under `[project]`, add the `cryptography` line to the `coordinator` list, and add the new `[dependency-groups]` table.)

Also add, anywhere after the existing tables:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Regenerate the lockfile**

Run:

```bash
uv lock
```

Expected: succeeds and updates `uv.lock` to include `websockets`, `cryptography`, `pytest`, `pytest-asyncio` and their transitive dependencies. (This resolves for the `[tool.uv].environments` target platform regardless of which OS you run `uv lock` from — it is not the same as `uv sync`, which is the command that actually fails on non-Linux.)

- [ ] **Step 3: Regenerate the plain-pip fallback lockfile**

Run:

```bash
uv export --extra node --no-emit-project -o requirements-node-lock.txt
```

Expected: succeeds, `requirements-node-lock.txt` now includes `websockets` alongside the existing `ray`/`vllm` pins (since `websockets` is now an unconditional project dependency, it's pulled in by the `--extra node` export too).

- [ ] **Step 4: Verify a local dev environment installs cleanly (this repo's non-Linux-safe pattern)**

Run, from the repo root:

```bash
uv venv --clear
uv pip install -e ".[coordinator]"
uv pip install pytest==9.1.1 pytest-asyncio==1.4.0
.venv/bin/python3 -c "import websockets, cryptography, mycelium; print(websockets.__version__, cryptography.__version__)"
.venv/bin/python3 -m pytest --version
```

Expected: the import line prints `17.0.1 50.0.0`; `pytest --version` prints without error. This installs `websockets` (unconditional) + `cryptography` (via the `coordinator` extra) without touching `ray`/`vllm` — deliberately avoided here since they're Linux-only and not needed for this plan's tests.

- [ ] **Step 5: Record the new pins in `docs/dependencies.md`**

`docs/dependencies.md` currently has one "Pinned versions" table scoped to `mycelium[node]`'s CUDA-heavy stack (`ray[llm]`, `vllm[audio]`). Add a second, separate table for the transport-layer deps this task adds, right after that existing table and before the "Reproducibility mechanism" paragraph (these two packages have no CUDA/hardware angle, so keep them out of the existing table rather than implying they do):

```markdown
## Pinned versions (`mycelium` base / `mycelium[coordinator]`)

| Package | Version | Why this exact version |
|---|---|---|
| `websockets` | 17.0.1 | Latest stable release (as of 2026-08-14) — unconditional project dependency, since both the node and coordinator need it. |
| `cryptography` | 50.0.0 | Latest stable release (as of 2026-08-14) — `mycelium[coordinator]` only, used for self-signed TLS cert generation (see [ADR-0002](adr/0002-node-transport-model.md) and the issue #5 design doc). |
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock requirements-node-lock.txt docs/dependencies.md
git commit -m "feat: add websockets/cryptography deps, pytest dev group, for coordinator-node transport"
```

---

### Task 2: Coordinator TLS certificate generation

**Files:**
- Create: `src/mycelium/coordinator/certs.py`
- Create: `tests/__init__.py`, `tests/coordinator/__init__.py`
- Create: `tests/coordinator/test_certs.py`

**Interfaces:**
- Consumes: `cryptography` from Task 1.
- Produces: `mycelium.coordinator.certs.ensure_cert(cert_path: Path, key_path: Path, ip: str) -> None` — generates a self-signed cert/key pair with `ip` as the Subject Alternative Name if `cert_path`/`key_path` don't both already exist; does nothing otherwise. Task 3 (server) and Task 5 (coordinator CLI) both call this.

- [ ] **Step 1: Write the failing tests**

Create `tests/__init__.py` and `tests/coordinator/__init__.py` (both empty files — needed so pytest can tell `tests/coordinator/test_cli.py` and `tests/node/test_cli.py` apart later).

Create `tests/coordinator/test_certs.py`:

```python
"""Tests for mycelium.coordinator.certs."""

import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from mycelium.coordinator.certs import ensure_cert


def test_generates_cert_and_key_files_when_missing(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_cert(cert_path, key_path, "20.244.2.48")

    assert cert_path.exists()
    assert key_path.exists()
    assert cert_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


def test_cert_has_correct_ip_san(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_cert(cert_path, key_path, "20.244.2.48")

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    ips = san.value.get_values_for_type(x509.IPAddress)
    assert ipaddress.ip_address("20.244.2.48") in ips


def test_does_not_regenerate_if_files_exist(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(b"existing-cert")
    key_path.write_bytes(b"existing-key")

    ensure_cert(cert_path, key_path, "20.244.2.48")

    assert cert_path.read_bytes() == b"existing-cert"
    assert key_path.read_bytes() == b"existing-key"


def test_key_matches_cert_public_key(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    ensure_cert(cert_path, key_path, "20.244.2.48")

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/coordinator/test_certs.py -v`
Expected: `ModuleNotFoundError: No module named 'mycelium.coordinator.certs'` (the module doesn't exist yet).

- [ ] **Step 3: Write `src/mycelium/coordinator/certs.py`**

```python
"""Self-signed TLS certificate generation for the coordinator.

Phase 0 has no public CA (see the design doc for issue #5) — the coordinator
generates one cert/key pair and every node pins that exact cert directly
(mycelium.node.connection), instead of validating against a CA chain.
"""

from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def ensure_cert(cert_path: Path, key_path: Path, ip: str) -> None:
    """Generate a self-signed cert/key pair at cert_path/key_path if missing.

    Does nothing if both files already exist. The generated cert's Subject
    Alternative Name is set to `ip` so a node pinning this exact cert file
    can validate the coordinator's identity without a public CA.
    """
    if cert_path.exists() and key_path.exists():
        return

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "mycelium-coordinator")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/coordinator/test_certs.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/__init__.py tests/coordinator/__init__.py tests/coordinator/test_certs.py src/mycelium/coordinator/certs.py
git commit -m "feat: coordinator self-signed cert generation"
```

---

### Task 3: Coordinator WebSocket server

**Files:**
- Create: `src/mycelium/coordinator/server.py`
- Create: `tests/coordinator/test_server.py`

**Interfaces:**
- Consumes: `mycelium.coordinator.certs.ensure_cert` (Task 2), for test fixtures only.
- Produces: `mycelium.coordinator.server.serve(host: str, port: int, cert_path: Path, key_path: Path)` — returns the object `websockets.serve(...)` returns (awaitable to get a `Server` directly, or usable as `async with serve(...) as server:`). Task 5 (coordinator CLI) calls this.

- [ ] **Step 1: Write the failing tests**

Create `tests/coordinator/test_server.py`:

```python
"""Tests for mycelium.coordinator.server."""

import ssl

import websockets

from mycelium.coordinator import certs, server


def _client_ssl_context(cert_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(cert_path))
    return context


async def test_node_can_connect_over_tls(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 8981, cert_path, key_path):
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect("wss://127.0.0.1:8981", ssl=client_ctx) as ws:
            assert ws.state.name == "OPEN"


async def test_multiple_nodes_can_connect_simultaneously(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 8982, cert_path, key_path):
        client_ctx = _client_ssl_context(cert_path)
        async with websockets.connect("wss://127.0.0.1:8982", ssl=client_ctx) as ws1:
            async with websockets.connect("wss://127.0.0.1:8982", ssl=client_ctx) as ws2:
                assert ws1.state.name == "OPEN"
                assert ws2.state.name == "OPEN"


async def test_connection_with_wrong_pinned_cert_is_rejected(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    # A second, unrelated cert stands in for "wrong pinned cert."
    other_cert_path = tmp_path / "other-cert.pem"
    other_key_path = tmp_path / "other-key.pem"
    certs.ensure_cert(other_cert_path, other_key_path, "127.0.0.1")

    async with server.serve("127.0.0.1", 8983, cert_path, key_path):
        wrong_ctx = _client_ssl_context(other_cert_path)
        try:
            async with websockets.connect("wss://127.0.0.1:8983", ssl=wrong_ctx):
                assert False, "expected connection to be rejected"
        except ssl.SSLCertVerificationError:
            pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/coordinator/test_server.py -v`
Expected: `ModuleNotFoundError: No module named 'mycelium.coordinator.server'`.

- [ ] **Step 3: Write `src/mycelium/coordinator/server.py`**

```python
"""WebSocket server the coordinator runs to accept dial-out node connections.

See ADR-0002 for why nodes dial out rather than the coordinator dialing in.
Phase 0 scope boundary (see the design doc for issue #5): this module only
holds connections open. It carries no registration/auth/routing logic —
that's issue #8 (registration) and #10 (routing).
"""

from __future__ import annotations

import ssl
from pathlib import Path

import websockets

PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20


async def _handle_node(websocket) -> None:
    """Hold a node's connection open. No business logic yet — see module docstring."""
    async for _message in websocket:
        pass


def build_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Build the server-side TLS context from the coordinator's cert/key pair."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def serve(host: str, port: int, cert_path: Path, key_path: Path):
    """Start the coordinator's node-facing WebSocket server.

    Returns whatever `websockets.serve` returns: awaitable to get a `Server`
    instance directly, or usable as `async with serve(...) as server:`.
    """
    ssl_context = build_ssl_context(cert_path, key_path)
    return websockets.serve(
        _handle_node,
        host,
        port,
        ssl=ssl_context,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/coordinator/test_server.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/coordinator/test_server.py src/mycelium/coordinator/server.py
git commit -m "feat: coordinator WebSocket server (TLS, node-facing)"
```

---

### Task 4: Node dial-out connection with reconnect/backoff

**Files:**
- Create: `src/mycelium/node/connection.py`
- Create: `tests/__init__.py` (already created in Task 2 — do not recreate; if it's missing when you start this task, something upstream broke)
- Create: `tests/node/__init__.py`
- Create: `tests/node/test_connection.py`

**Interfaces:**
- Consumes: `mycelium.coordinator.certs.ensure_cert` (Task 2, test fixtures only) and raw `websockets.serve` (test fixtures only — deliberately not `mycelium.coordinator.server.serve`, to keep this module's tests independent of the coordinator package's internals).
- Produces: `mycelium.node.connection.connect(coordinator_url: str, coordinator_cert_path: Path, *, reconnect_delays_factory=reconnect_delays)` — returns a `websockets.connect(...)` reconnecting async iterator: `async for websocket in connect(...): ...`. Also produces `mycelium.node.connection.reconnect_delays` (the default backoff generator factory, importable so Task 5's CLI can use it as the default without redefining it). Task 5 (node CLI) calls `connect`.

- [ ] **Step 1: Write the failing tests**

Create `tests/node/__init__.py` (empty file).

Create `tests/node/test_connection.py`:

```python
"""Tests for mycelium.node.connection."""

import asyncio
import itertools
import ssl

import websockets

from mycelium.coordinator import certs
from mycelium.node import connection


def _server_ssl_context(cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


async def _echo_handler(websocket):
    async for message in websocket:
        await websocket.send(message)


def test_reconnect_delays_starts_near_one_second():
    delays = list(itertools.islice(connection.reconnect_delays(), 1))
    assert 0.8 <= delays[0] <= 1.2


def test_reconnect_delays_grows_and_caps_at_thirty_seconds():
    delays = list(itertools.islice(connection.reconnect_delays(), 10))
    # Roughly doubling early on (within jitter bounds)...
    assert 1.6 <= delays[1] <= 2.4
    assert 3.2 <= delays[2] <= 4.8
    # ...and capped at ~30s by the time it's had several steps to grow there.
    assert 24.0 <= delays[-1] <= 36.0


async def test_connects_to_real_server(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(_echo_handler, "127.0.0.1", 8991, ssl=server_ctx):
        async for websocket in connection.connect("wss://127.0.0.1:8991", cert_path):
            assert websocket.state.name == "OPEN"
            break  # one connection is enough to prove this works


async def test_reconnects_after_server_restart(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    server_ctx = _server_ssl_context(cert_path, key_path)

    def fast_delays():
        while True:
            yield 0.1

    connect_count = 0

    async def client_loop():
        nonlocal connect_count
        async for websocket in connection.connect(
            "wss://127.0.0.1:8992", cert_path, reconnect_delays_factory=fast_delays
        ):
            connect_count += 1
            try:
                await websocket.wait_closed()
            except websockets.exceptions.ConnectionClosed:
                continue

    client_task = asyncio.create_task(client_loop())

    server1 = await websockets.serve(_echo_handler, "127.0.0.1", 8992, ssl=server_ctx)
    await asyncio.sleep(1)
    server1.close()
    await server1.wait_closed()

    await asyncio.sleep(1.5)  # let the client notice the drop and start retrying

    server2 = await websockets.serve(_echo_handler, "127.0.0.1", 8992, ssl=server_ctx)
    await asyncio.sleep(1.5)  # let the client reconnect

    client_task.cancel()
    server2.close()
    await server2.wait_closed()

    assert connect_count >= 2, f"expected at least 2 connect attempts, got {connect_count}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/node/test_connection.py -v`
Expected: `ModuleNotFoundError: No module named 'mycelium.node.connection'`.

- [ ] **Step 3: Write `src/mycelium/node/connection.py`**

```python
"""Dial-out connection from a node to the coordinator.

See ADR-0002: the node initiates and holds the connection open; the
coordinator never dials into a node. Phase 0 scope boundary (see the design
doc for issue #5): this module only connects and reconnects. It sends no
node identity, token, or registration message — that's issue #8.
"""

from __future__ import annotations

import random
import ssl
from collections.abc import Generator
from pathlib import Path

import websockets

PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 20

INITIAL_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
BACKOFF_FACTOR = 2.0
JITTER_FRACTION = 0.2


def reconnect_delays() -> Generator[float, None, None]:
    """Exponential backoff with jitter: 1s initial, x2 each step, capped at 30s, +/-20%."""
    delay = INITIAL_DELAY_SECONDS
    while True:
        jitter = delay * JITTER_FRACTION * (2 * random.random() - 1)
        yield max(0.0, delay + jitter)
        delay = min(delay * BACKOFF_FACTOR, MAX_DELAY_SECONDS)


def build_ssl_context(coordinator_cert_path: Path) -> ssl.SSLContext:
    """Build the client-side TLS context that trusts only the coordinator's pinned cert."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=str(coordinator_cert_path))
    return context


def connect(
    coordinator_url: str,
    coordinator_cert_path: Path,
    *,
    reconnect_delays_factory=reconnect_delays,
):
    """Dial out to the coordinator, reconnecting automatically on drop.

    Returns a `websockets.connect(...)` reconnecting async iterator:
    `async for websocket in connect(...): ...` yields a new, already-open
    connection each time the previous one closes, after waiting according
    to `reconnect_delays_factory`.
    """
    ssl_context = build_ssl_context(coordinator_cert_path)
    return websockets.connect(
        coordinator_url,
        ssl=ssl_context,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
        reconnect_delays=reconnect_delays_factory,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/node/test_connection.py -v`
Expected: 4 passed. (`test_reconnects_after_server_restart` takes a few seconds — it's a real timed test, not mocked; that's deliberate, matching this issue's "verify for real" bar.)

- [ ] **Step 5: Commit**

```bash
git add tests/node/__init__.py tests/node/test_connection.py src/mycelium/node/connection.py
git commit -m "feat: node dial-out connection with reconnect/backoff"
```

---

### Task 5: CLI wiring

**Files:**
- Modify: `src/mycelium/coordinator/cli.py`
- Modify: `src/mycelium/node/cli.py`
- Create: `tests/coordinator/test_cli.py`
- Create: `tests/node/test_cli.py`

**Interfaces:**
- Consumes: `mycelium.coordinator.certs.ensure_cert` (Task 2), `mycelium.coordinator.server.serve` (Task 3), `mycelium.node.connection.connect` (Task 4).
- Produces: the `mycelium-coordinator` and `mycelium-node` console-script entry points (already wired in `pyproject.toml`'s `[project.scripts]` from issue #1) now do real work instead of printing a stub line. Nothing downstream in this plan depends on this task's internals — it's the last coded task.

- [ ] **Step 1: Write the failing tests**

Create `tests/coordinator/test_cli.py`:

```python
"""Tests for mycelium.coordinator.cli."""

import pytest

from mycelium.coordinator.cli import parse_args, _run


def test_parse_args_defaults():
    args = parse_args([])
    assert args.host == "0.0.0.0"
    assert args.port == 8765


def test_parse_args_overrides():
    args = parse_args(["--host", "127.0.0.1", "--port", "9000"])
    assert args.host == "127.0.0.1"
    assert args.port == 9000


async def test_run_requires_cert_san_ip_when_no_existing_cert(tmp_path):
    args = parse_args(
        [
            "--cert-file", str(tmp_path / "cert.pem"),
            "--key-file", str(tmp_path / "key.pem"),
        ]
    )
    with pytest.raises(SystemExit):
        await _run(args)
```

Create `tests/node/test_cli.py`:

```python
"""Tests for mycelium.node.cli."""

import pytest

from mycelium.node.cli import parse_args


def test_parse_args_requires_coordinator_url():
    with pytest.raises(SystemExit):
        parse_args(["--coordinator-cert", "/tmp/cert.pem"])


def test_parse_args_requires_coordinator_cert():
    with pytest.raises(SystemExit):
        parse_args(["--coordinator-url", "wss://example:8765"])


def test_parse_args_valid():
    args = parse_args(
        ["--coordinator-url", "wss://example:8765", "--coordinator-cert", "/tmp/cert.pem"]
    )
    assert args.coordinator_url == "wss://example:8765"
    assert str(args.coordinator_cert) == "/tmp/cert.pem"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/coordinator/test_cli.py tests/node/test_cli.py -v`
Expected: failures — `parse_args`/`_run` don't exist yet in either `cli.py` (the current files only define `main()`, per issue #1's stub).

- [ ] **Step 3: Rewrite `src/mycelium/coordinator/cli.py`**

```python
"""CLI entry point for the Mycelium coordinator."""

from __future__ import annotations

import argparse
import asyncio
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

    print(f"mycelium-coordinator {__version__} listening on {args.host}:{args.port}")
    async with server.serve(args.host, args.port, args.cert_file, args.key_file):
        await asyncio.Future()  # run forever


def main() -> None:
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Rewrite `src/mycelium/node/cli.py`**

```python
"""CLI entry point for the Mycelium node agent."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mycelium import __version__
from mycelium.node import connection


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mycelium-node")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-cert", type=Path, required=True)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    print(f"mycelium-node {__version__} connecting to {args.coordinator_url}")
    async for websocket in connection.connect(args.coordinator_url, args.coordinator_cert):
        print(f"connected to coordinator ({args.coordinator_url})")
        try:
            await websocket.wait_closed()
            print("connection to coordinator closed, reconnecting...")
        except Exception:
            continue


def main() -> None:
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/coordinator/test_cli.py tests/node/test_cli.py -v`
Expected: 6 passed.

- [ ] **Step 6: Run the full test suite so far**

Run: `.venv/bin/python3 -m pytest -v`
Expected: all tests from Tasks 2–5 pass (17 tests total: 4 certs + 3 server + 4 connection + 3 coordinator-CLI tests + 3 node-CLI tests — count them as they run and confirm nothing regressed).

- [ ] **Step 7: Commit**

```bash
git add src/mycelium/coordinator/cli.py src/mycelium/node/cli.py tests/coordinator/test_cli.py tests/node/test_cli.py
git commit -m "feat: wire coordinator/node CLIs to real transport (cert gen, serve, connect)"
```

---

### Task 6: Local end-to-end integration test

**Files:**
- Create: `tests/test_integration.py`

**Interfaces:**
- Consumes: `mycelium.coordinator.certs`, `mycelium.coordinator.server`, `mycelium.node.connection` (Tasks 2–4) — deliberately the lower-level modules, not the CLIs, so this test isn't dependent on `sys.argv`/subprocess plumbing to prove the pieces work together.
- Produces: nothing consumed elsewhere — this is the last automated test in the plan, proving the full local stack (cert → server → client → reconnect) works together in one test, not just in isolation per-module.

- [ ] **Step 1: Write the failing test**

Create `tests/test_integration.py`:

```python
"""End-to-end local integration test: cert generation, server, and the node's
reconnecting client, all together. Real two-machine verification (a real
coordinator host and real node hardware) happens separately — see the
design doc and plan for issue #5 — this test only proves the pieces wire
up correctly on localhost before that.
"""

import asyncio

import websockets

from mycelium.coordinator import certs, server
from mycelium.node import connection


async def test_node_connects_survives_a_ping_cycle_and_reconnects_after_drop(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    def fast_delays():
        while True:
            yield 0.1

    connect_count = 0
    stop = asyncio.Event()

    async def node_loop():
        nonlocal connect_count
        async for websocket in connection.connect(
            "wss://127.0.0.1:8995", cert_path, reconnect_delays_factory=fast_delays
        ):
            connect_count += 1
            try:
                await websocket.wait_closed()
            except websockets.exceptions.ConnectionClosed:
                if stop.is_set():
                    return
                continue

    node_task = asyncio.create_task(node_loop())

    coordinator1 = await server.serve("127.0.0.1", 8995, cert_path, key_path)
    await asyncio.sleep(0.5)
    assert connect_count == 1, "node should have connected once to the first coordinator"

    coordinator1.close()
    await coordinator1.wait_closed()
    await asyncio.sleep(0.5)  # let the node notice the drop

    coordinator2 = await server.serve("127.0.0.1", 8995, cert_path, key_path)
    await asyncio.sleep(0.5)  # let the node reconnect

    stop.set()
    node_task.cancel()
    coordinator2.close()
    await coordinator2.wait_closed()

    assert connect_count == 2, f"expected exactly 2 connect attempts, got {connect_count}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_integration.py -v`
Expected: fails only if something from Tasks 2–4 is missing or broken — if those tasks are done correctly, this test should actually pass immediately on first run, since it only composes already-implemented, already-tested pieces. If it fails, that's a real integration bug (a mismatch between how `server.serve` and `connection.connect` are meant to be used together) — debug it using superpowers:systematic-debugging rather than patching the test to hide the failure.

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/python3 -m pytest -v`
Expected: 18 passed (all 17 of Tasks 2–5's tests, plus this one).

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: local end-to-end integration test for coordinator-node transport"
```

---

## After Task 6: real two-machine verification (controller-performed, not a dispatched task)

This is required by issue #5's acceptance criteria ("tested across two separate real machines, not localhost-only") and is **not satisfied by Task 6**, which only proves the code works on localhost. Perform this directly after Task 6's review comes back clean, before the final whole-branch review:

1. On the Azure VM (`azureuser@20.244.2.48`): install the project (`git clone` + the `uv venv` / `uv pip install -e ".[coordinator]"` pattern from Task 1), then run
   `mycelium-coordinator --host 0.0.0.0 --port 8765 --cert-san-ip 20.244.2.48 --cert-file ~/.mycelium/coordinator-cert.pem --key-file ~/.mycelium/coordinator-key.pem`.
   Confirm port 8765 is actually reachable from outside first (`nc -zv 20.244.2.48 8765` from this Mac) — if the operator's opened NSG range doesn't include 8765, pick a port that is open and use that instead; there's nothing special about 8765.
2. `scp` the coordinator's public cert (`~/.mycelium/coordinator-cert.pem` — **not** the key file) from the Azure VM to `a6000` and separately to `h100`.
3. On `a6000`: install the project the same way, then run
   `mycelium-node --coordinator-url wss://20.244.2.48:8765 --coordinator-cert ~/coordinator-cert.pem`. Confirm the "connected to coordinator" line prints.
4. Repeat step 3 on `h100`, run concurrently with `a6000`'s connection — confirm the coordinator's terminal/logs show both connections accepted (Task 3's server already supports multiple simultaneous connections; this confirms it over the real network path, not just localhost).
5. Idle test: leave both connections open, undisturbed, for at least 5 minutes. Confirm neither side reports a drop (this is the real test of the "survives being idle" acceptance criterion, across the actual VPN + Azure NSG path — not just localhost's default OS networking, which was already implicitly proven trivial by Task 6).
6. Reconnect test: kill the coordinator process on the Azure VM. Confirm both nodes' processes stay running and start retrying (visible in their own output). Restart the coordinator. Confirm both nodes reconnect without manual intervention.
7. Record the outcome (what was run, what was observed, any deviations from the plan — e.g. a different port than 8765) in the final whole-branch review's context, the same way issue #4's live evidence was captured, so the final review and the eventual PR description have real evidence to point to, not just "it passed."
