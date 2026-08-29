# Issue #34 — Node-Side GitHub Device-Flow Sign-In Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Running `mycelium-node` with no usable GitHub token drives GitHub's OAuth device flow itself — prints a code, polls until the volunteer authorizes it in a browser on any device, then registers with the resulting token, exactly the way a hand-supplied `--github-token-file` already does today.

**Architecture:** A new `src/mycelium/node/github_device_flow.py` module (mirroring `coordinator/github_identity.py`'s real-implementation-plus-injectable-fake pattern) does the two blocking HTTP calls (`request_device_code`, `poll_once`) against GitHub's own endpoints. `node/cli.py` gains an async polling orchestrator (`_authenticate`) that calls that client via `asyncio.to_thread` and prints volunteer-facing instructions, plus wiring in `_run()` that gives `--github-token-file` a default cache path (mirroring `identity.load_or_create_keypair`'s generate-if-missing idiom) so device flow only ever runs once per node.

**Tech Stack:** Python 3.11, stdlib `urllib`/`webbrowser` (no new dependencies), `pytest`/`pytest-asyncio` (see existing node/coordinator test modules).

## Global Constraints

- `CLIENT_ID` in `github_device_flow.py` is a placeholder constant, not an env-var override — the real GitHub App is registered by the operator later, out of band.
- `request_device_code()` fails fast with `DeviceFlowConfigError` (no network call) if `CLIENT_ID` still equals the placeholder sentinel.
- `--github-token-file` defaults to `~/.mycelium/github-token`. Exists → read verbatim, device flow never runs. Missing *and the flag was left unset* → device flow runs, result cached to that path (`chmod 600`). Missing *and the flag was explicitly passed* → hard `SystemExit` immediately, never a device-flow fallback.
- Device flow runs before `vLLM` starts — this requires no structural reorder, since the token-resolution block already sits above `process.start()` in today's code.
- Polling: `authorization_pending` → keep polling at the current interval; `slow_down` → use GitHub's returned `interval` (fallback `interval + 5` only if GitHub omits it); `expired_token` → transparently request a fresh code and keep looping; `access_denied` → `SystemExit`, telling the operator to re-run `mycelium-node`; any other/unrecognized error code → `SystemExit` with the raw error (config/programming-bug signals, not transient states).
- OAuth `scope` requested: none (empty) — confirmed optional against GitHub's docs.
- A registration rejection whose reason is exactly `"invalid or expired GitHub token"` (server.py's exact string) gets a distinguishing retry-log hint pointing at the cache file to delete — still retries forever like every other rejection reason, no auto-recovery.
- Seam shape: a duck-typed `client` (`request_device_code()`, `poll_once(device_code, interval)`) defaulting to the real `github_device_flow` module, plus an injectable `sleep` — both threaded through as `_run()` parameters, mirroring how `process: VLLMProcess` is already injected into `_run()`.
- Best-effort `webbrowser.open(verification_uri)` on top of always-printed instructions, wrapped in a broad `try/except` so a headless box never surfaces an error from it.
- No env-var `CLIENT_ID` override, no auto-recovery from a stale cached token beyond the diagnostic hint, no change to the `--prompt`-only standalone mode, no coordinator-side changes (#39 already accepts `github_token` on `register`).

---

### Task 1: `github_device_flow.py` — real device-flow client

**Files:**
- Create: `src/mycelium/node/github_device_flow.py`
- Test: `tests/node/test_github_device_flow.py`

**Interfaces:**
- Consumes: nothing project-internal — talks directly to `https://github.com/login/device/code` and `https://github.com/login/oauth/access_token` via `urllib.request`.
- Produces (consumed by Task 2): `@dataclass(frozen=True) class DeviceCode(device_code: str, user_code: str, verification_uri: str, expires_in: int, interval: int)`; `@dataclass(frozen=True) class PollResult(token: str | None, interval: int)`; `class DeviceFlowError(Exception)`; `class AuthorizationDenied(DeviceFlowError)`; `class DeviceCodeExpired(DeviceFlowError)`; `class DeviceFlowConfigError(DeviceFlowError)`; `def request_device_code() -> DeviceCode` (raises `DeviceFlowConfigError`); `def poll_once(device_code: str, interval: int) -> PollResult` (raises `DeviceCodeExpired`, `AuthorizationDenied`, `DeviceFlowConfigError`); module constant `CLIENT_ID: str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/node/test_github_device_flow.py`:

```python
"""Tests for mycelium.node.github_device_flow."""

import io
import json
import urllib.error
import urllib.request

from mycelium.node import github_device_flow


class _FakeResponse:
    """Stand-in for the context-manager urlopen() returns on a 2xx
    response — mirrors tests/coordinator/test_github_identity.py's
    identical helper."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    """A real HTTPError carrying a readable JSON body — matches how
    urllib.request.urlopen actually raises for a non-2xx response with a
    body (GitHub's device-flow token endpoint returns its
    authorization_pending/slow_down/expired_token/access_denied errors
    this way), so _post_form's exc.read() path is exercised against real
    urllib behavior, not a stand-in."""
    return urllib.error.HTTPError(
        github_device_flow.GITHUB_TOKEN_URL, code, "error", {},
        io.BytesIO(json.dumps(body).encode()),
    )


def test_request_device_code_fails_fast_without_network_call_when_client_id_is_placeholder(
    monkeypatch,
):
    def fail_if_called(request, timeout):
        raise AssertionError("must not make a network call with the placeholder client_id")

    monkeypatch.setattr(urllib.request, "urlopen", fail_if_called)

    try:
        github_device_flow.request_device_code()
        assert False, "expected DeviceFlowConfigError"
    except github_device_flow.DeviceFlowConfigError as exc:
        assert "not configured" in str(exc)


def test_request_device_code_returns_parsed_fields_on_success(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")

    def fake_urlopen(request, timeout):
        assert request.full_url == github_device_flow.GITHUB_DEVICE_CODE_URL
        return _FakeResponse({
            "device_code": "devcode123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    device = github_device_flow.request_device_code()

    assert device == github_device_flow.DeviceCode(
        device_code="devcode123", user_code="ABCD-1234",
        verification_uri="https://github.com/login/device",
        expires_in=900, interval=5,
    )


def test_request_device_code_passes_the_configured_timeout(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        return _FakeResponse({
            "device_code": "d", "user_code": "u", "verification_uri": "v",
            "expires_in": 900, "interval": 5,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    github_device_flow.request_device_code()

    assert captured["timeout"] == github_device_flow.REQUEST_TIMEOUT_SECONDS


def test_request_device_code_raises_config_error_on_malformed_response(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: _FakeResponse({"unexpected": "shape"})
    )

    try:
        github_device_flow.request_device_code()
        assert False, "expected DeviceFlowConfigError"
    except github_device_flow.DeviceFlowConfigError:
        pass


def test_poll_once_returns_token_on_success(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout: _FakeResponse({"access_token": "gho_abc123", "token_type": "bearer"}),
    )

    result = github_device_flow.poll_once("devcode123", interval=5)

    assert result == github_device_flow.PollResult(token="gho_abc123", interval=5)


def test_poll_once_returns_none_token_on_authorization_pending(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")

    def fake_urlopen(request, timeout):
        raise _http_error(400, {"error": "authorization_pending"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = github_device_flow.poll_once("devcode123", interval=5)

    assert result == github_device_flow.PollResult(token=None, interval=5)


def test_poll_once_uses_the_servers_new_interval_on_slow_down(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")

    def fake_urlopen(request, timeout):
        raise _http_error(400, {"error": "slow_down", "interval": 12})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = github_device_flow.poll_once("devcode123", interval=5)

    assert result == github_device_flow.PollResult(token=None, interval=12)


def test_poll_once_falls_back_to_plus_five_when_slow_down_omits_interval(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")

    def fake_urlopen(request, timeout):
        raise _http_error(400, {"error": "slow_down"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = github_device_flow.poll_once("devcode123", interval=5)

    assert result == github_device_flow.PollResult(token=None, interval=10)


def test_poll_once_raises_device_code_expired(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")

    def fake_urlopen(request, timeout):
        raise _http_error(400, {"error": "expired_token"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        github_device_flow.poll_once("devcode123", interval=5)
        assert False, "expected DeviceCodeExpired"
    except github_device_flow.DeviceCodeExpired:
        pass


def test_poll_once_raises_authorization_denied(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")

    def fake_urlopen(request, timeout):
        raise _http_error(400, {"error": "access_denied"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        github_device_flow.poll_once("devcode123", interval=5)
        assert False, "expected AuthorizationDenied"
    except github_device_flow.AuthorizationDenied:
        pass


def test_poll_once_raises_config_error_on_unrecognized_error_code(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")

    def fake_urlopen(request, timeout):
        raise _http_error(400, {"error": "incorrect_client_credentials"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        github_device_flow.poll_once("devcode123", interval=5)
        assert False, "expected DeviceFlowConfigError"
    except github_device_flow.DeviceFlowConfigError:
        pass


def test_poll_once_raises_config_error_on_network_failure(monkeypatch):
    monkeypatch.setattr(github_device_flow, "CLIENT_ID", "test-client-id")

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        github_device_flow.poll_once("devcode123", interval=5)
        assert False, "expected DeviceFlowConfigError"
    except github_device_flow.DeviceFlowConfigError:
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_github_device_flow.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mycelium.node.github_device_flow'`.

- [ ] **Step 3: Implement**

Create `src/mycelium/node/github_device_flow.py`:

```python
"""Real GitHub OAuth device-flow implementation for node registration.

Drives GitHub's own device-authorization endpoints directly — no
coordinator involvement in the OAuth dance. See the design doc for issue
#34. Tests never call this module's real request_device_code/poll_once —
node/cli.py's _authenticate takes an injectable client instead, so no
test ever makes a real network call to GitHub, matching the pattern
coordinator/github_identity.py already established for issue #39.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

# Not secret — ships embedded in the mycelium-node package, the same way
# tools like the GitHub CLI embed their own device-flow client_id. This is
# a placeholder until the real GitHub App is registered (a follow-up,
# out-of-band operator action — see the design doc for issue #34 and
# docs/OPERATIONS.md).
CLIENT_ID = "REPLACE_ME_WITH_REAL_GITHUB_APP_CLIENT_ID"

# Bounds each blocking HTTP call, same reasoning as
# coordinator/github_identity.VERIFY_TIMEOUT_SECONDS.
REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class PollResult:
    # None means "still pending, keep polling" — a real string means the
    # flow succeeded.
    token: str | None
    # The interval to use for the *next* poll — unchanged unless GitHub's
    # slow_down response said otherwise.
    interval: int


class DeviceFlowError(Exception):
    """Base class for all device-flow failures."""


class AuthorizationDenied(DeviceFlowError):
    """The volunteer clicked Cancel during authorization (access_denied)."""


class DeviceCodeExpired(DeviceFlowError):
    """The device/user code's TTL elapsed (expired_token) — the caller
    should request a fresh device code and keep going."""


class DeviceFlowConfigError(DeviceFlowError):
    """A configuration-level problem, not a transient polling state:
    CLIENT_ID is still the shipped placeholder, or GitHub itself reports
    one of unsupported_grant_type / incorrect_client_credentials /
    incorrect_device_code / device_flow_disabled, or any other
    unrecognized error code, or the request couldn't be completed at all.
    Retrying won't fix any of these — see the design doc for issue #34."""


def _post_form(url: str, fields: dict[str, str]) -> dict:
    """Blocking form-encoded POST, JSON response. GitHub's device-flow
    endpoints respond to polling errors (authorization_pending etc.) with
    a non-2xx HTTP status carrying a JSON body — this always attempts to
    parse a JSON body off the response, whether urlopen returns it
    directly or raises HTTPError, rather than assuming a non-2xx status
    means the body isn't worth reading. Only a body that can't be parsed
    at all, or a genuine network failure, becomes DeviceFlowConfigError.
    Only ever run via asyncio.to_thread, never called directly from an
    async context."""
    body_bytes = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url,
        data=body_bytes,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "mycelium-node",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read())
        except (ValueError, OSError) as parse_exc:
            raise DeviceFlowConfigError(
                f"unexpected GitHub response: HTTP {exc.code}"
            ) from parse_exc
    except (OSError, ValueError) as exc:
        raise DeviceFlowConfigError(f"could not reach GitHub: {exc}") from exc


def request_device_code() -> DeviceCode:
    """POST /login/device/code. Raises DeviceFlowConfigError immediately,
    with no network call, if CLIENT_ID is still the shipped placeholder —
    see the design doc for issue #34 on why this fails fast instead of
    letting a misconfigured deployment fail deep inside the polling loop
    with GitHub's opaque incorrect_client_credentials."""
    if CLIENT_ID == "REPLACE_ME_WITH_REAL_GITHUB_APP_CLIENT_ID":
        raise DeviceFlowConfigError(
            "mycelium-node's GitHub App is not configured yet — see docs/OPERATIONS.md"
        )
    body = _post_form(GITHUB_DEVICE_CODE_URL, {"client_id": CLIENT_ID})
    try:
        return DeviceCode(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body["verification_uri"],
            expires_in=body["expires_in"],
            interval=body["interval"],
        )
    except KeyError as exc:
        raise DeviceFlowConfigError(f"unexpected GitHub response shape: {body!r}") from exc


def poll_once(device_code: str, interval: int) -> PollResult:
    """POST /login/oauth/access_token once. Returns PollResult(token=None,
    interval=...) for authorization_pending/slow_down (caller keeps
    polling — slow_down updates interval to GitHub's requested value, or
    interval + 5 if GitHub omits it). Raises DeviceCodeExpired for
    expired_token, AuthorizationDenied for access_denied,
    DeviceFlowConfigError for anything else."""
    body = _post_form(
        GITHUB_TOKEN_URL,
        {
            "client_id": CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    if "access_token" in body:
        return PollResult(token=body["access_token"], interval=interval)

    error = body.get("error")
    if error == "authorization_pending":
        return PollResult(token=None, interval=interval)
    if error == "slow_down":
        return PollResult(token=None, interval=body.get("interval", interval + 5))
    if error == "expired_token":
        raise DeviceCodeExpired("device code expired")
    if error == "access_denied":
        raise AuthorizationDenied("authorization was denied")
    raise DeviceFlowConfigError(f"unexpected response from GitHub: {body!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_github_device_flow.py -v`
Expected: PASS (all 11 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/github_device_flow.py tests/node/test_github_device_flow.py
git commit -m "feat: add github_device_flow real device-flow client (issue #34)"
```

---

### Task 2: `node/cli.py` — `_authenticate` polling orchestrator

**Files:**
- Modify: `src/mycelium/node/cli.py`
- Test: `tests/node/test_cli.py`

**Interfaces:**
- Consumes (from Task 1): `github_device_flow.DeviceCode`, `github_device_flow.PollResult`, `github_device_flow.DeviceCodeExpired`, `github_device_flow.AuthorizationDenied`, `github_device_flow.DeviceFlowConfigError`, `github_device_flow.request_device_code`, `github_device_flow.poll_once` (as attributes of a duck-typed `client`).
- Produces (consumed by Task 3): `async def _request_and_print_code(client) -> github_device_flow.DeviceCode` (raises `SystemExit`); `async def _authenticate(client=github_device_flow, sleep=asyncio.sleep) -> str` (raises `SystemExit`).

- [ ] **Step 1: Write the failing tests**

Add near the top of `tests/node/test_cli.py`, right after the existing imports (after `from mycelium.node.cli import _run, parse_args`):

```python
from mycelium.node import github_device_flow
from mycelium.node.cli import _authenticate
```

Append to `tests/node/test_cli.py`:

```python
class _FakeDeviceFlowClient:
    """Scripted device-flow client for _authenticate tests — no real
    network calls, no real waiting.

    `device_or_devices` is either a single DeviceCode (returned from every
    request_device_code() call — the common case) or a list of DeviceCode
    (returned one per call, the last entry repeating once exhausted — only
    the expired-token test needs this, to prove a fresh code was actually
    requested and used).

    `poll_responses` is a list consumed one entry per poll_once() call;
    each entry is either a PollResult or an exception instance to raise.
    """

    def __init__(self, device_or_devices, poll_responses: list):
        devices = (
            device_or_devices if isinstance(device_or_devices, list) else [device_or_devices]
        )
        self._devices = list(devices)
        self.poll_responses = list(poll_responses)
        self.request_device_code_calls = 0
        self.poll_once_calls = []

    def request_device_code(self):
        self.request_device_code_calls += 1
        if len(self._devices) > 1:
            return self._devices.pop(0)
        return self._devices[0]

    def poll_once(self, device_code, interval):
        self.poll_once_calls.append((device_code, interval))
        response = self.poll_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


async def _no_op_sleep(seconds):
    pass


def _device_code(**overrides):
    fields = dict(
        device_code="devcode123", user_code="ABCD-1234",
        verification_uri="https://github.com/login/device",
        expires_in=900, interval=5,
    )
    fields.update(overrides)
    return github_device_flow.DeviceCode(**fields)


async def test_authenticate_prints_code_and_returns_token_on_success(capsys):
    device = _device_code()
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token=None, interval=5),
                 github_device_flow.PollResult(token="gho_abc123", interval=5)]
    )

    token = await _authenticate(client=client, sleep=_no_op_sleep)

    assert token == "gho_abc123"
    out = capsys.readouterr().out
    assert "ABCD-1234" in out
    assert "https://github.com/login/device" in out


async def test_authenticate_keeps_polling_through_authorization_pending():
    device = _device_code()
    client = _FakeDeviceFlowClient(
        device,
        [github_device_flow.PollResult(token=None, interval=5)] * 3
        + [github_device_flow.PollResult(token="gho_final", interval=5)],
    )

    token = await _authenticate(client=client, sleep=_no_op_sleep)

    assert token == "gho_final"
    assert len(client.poll_once_calls) == 4


async def test_authenticate_uses_updated_interval_after_slow_down():
    device = _device_code(interval=5)
    client = _FakeDeviceFlowClient(
        device,
        [github_device_flow.PollResult(token=None, interval=12),
         github_device_flow.PollResult(token="gho_abc", interval=12)],
    )

    await _authenticate(client=client, sleep=_no_op_sleep)

    # First poll uses the device's initial interval (5); the second poll
    # must use the interval slow_down returned (12), not the original 5.
    assert client.poll_once_calls[0][1] == 5
    assert client.poll_once_calls[1][1] == 12


async def test_authenticate_requests_a_fresh_code_after_expired_token(capsys):
    first_device = _device_code(user_code="OLD-CODE", device_code="old-devcode")
    second_device = _device_code(user_code="NEW-CODE", device_code="new-devcode")
    # Two devices in sequence: request_device_code() returns first_device
    # on the first call (consumed by the initial code print), then
    # second_device on the second call (the retry after DeviceCodeExpired)
    # — proving _authenticate actually requested and used a fresh code
    # rather than reusing the expired one.
    client = _FakeDeviceFlowClient(
        [first_device, second_device],
        [github_device_flow.DeviceCodeExpired(), github_device_flow.PollResult(token="gho_x", interval=5)],
    )

    token = await _authenticate(client=client, sleep=_no_op_sleep)

    assert token == "gho_x"
    assert client.request_device_code_calls == 2
    assert client.poll_once_calls[-1][0] == "new-devcode"
    out = capsys.readouterr().out
    assert "OLD-CODE" in out
    assert "NEW-CODE" in out


async def test_authenticate_exits_on_authorization_denied():
    device = _device_code()
    client = _FakeDeviceFlowClient(device, [github_device_flow.AuthorizationDenied()])

    with pytest.raises(SystemExit, match="denied"):
        await _authenticate(client=client, sleep=_no_op_sleep)


async def test_authenticate_exits_on_device_flow_config_error_from_poll():
    device = _device_code()
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.DeviceFlowConfigError("unexpected response from GitHub")]
    )

    with pytest.raises(SystemExit, match="unexpected response"):
        await _authenticate(client=client, sleep=_no_op_sleep)


async def test_authenticate_exits_on_device_flow_config_error_from_initial_request():
    class _FailingClient:
        def request_device_code(self):
            raise github_device_flow.DeviceFlowConfigError("not configured yet")

    with pytest.raises(SystemExit, match="not configured"):
        await _authenticate(client=_FailingClient(), sleep=_no_op_sleep)


async def test_authenticate_opens_browser_to_verification_uri(monkeypatch):
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    device = _device_code(verification_uri="https://github.com/login/device")
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token="gho_abc", interval=5)]
    )

    await _authenticate(client=client, sleep=_no_op_sleep)

    assert opened == ["https://github.com/login/device"]


async def test_authenticate_swallows_browser_open_failures(monkeypatch, capsys):
    import webbrowser

    def raising_open(url):
        raise RuntimeError("no display available")

    monkeypatch.setattr(webbrowser, "open", raising_open)

    device = _device_code()
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token="gho_abc", interval=5)]
    )

    # Must not raise — a headless box without a display must never see an
    # error from the best-effort browser-open convenience.
    token = await _authenticate(client=client, sleep=_no_op_sleep)

    assert token == "gho_abc"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_cli.py -k authenticate -v`
Expected: FAIL with `ImportError: cannot import name '_authenticate' from 'mycelium.node.cli'`.

- [ ] **Step 3: Implement**

In `src/mycelium/node/cli.py`, update the import block. Change:

```python
import argparse
import asyncio
import logging
import signal
import socket
import sys
from pathlib import Path

from mycelium import __version__
from mycelium import crypto
from mycelium.node import connection, identity, registration, request_handler
```

to:

```python
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
```

(This is the only import-block change this whole plan makes — Task 3 does not touch it again.)

Insert two new functions between `parse_args` and `_run` (i.e. right after `parse_args`'s closing `return args`, before `async def _run(...)`):

```python
async def _request_and_print_code(client) -> github_device_flow.DeviceCode:
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
        webbrowser.open(device.verification_uri)
    except Exception:
        pass
    return device


async def _authenticate(client=github_device_flow, sleep=asyncio.sleep) -> str:
    """Drive GitHub's OAuth device flow to completion and return the
    resulting access token. Only ever called when no usable GitHub token
    was found on disk — see _run(). See the design doc for issue #34 for
    the full polling-error decision table."""
    device = await _request_and_print_code(client)
    interval = device.interval
    while True:
        await sleep(interval)
        try:
            result = await asyncio.to_thread(client.poll_once, device.device_code, interval)
        except github_device_flow.DeviceCodeExpired:
            print("Code expired, requesting a new one...", flush=True)
            device = await _request_and_print_code(client)
            interval = device.interval
            continue
        except github_device_flow.AuthorizationDenied:
            raise SystemExit("GitHub sign-in was denied. Re-run mycelium-node to try again.")
        except github_device_flow.DeviceFlowConfigError as exc:
            raise SystemExit(f"error: {exc}")
        interval = result.interval
        if result.token is not None:
            return result.token
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_cli.py -v`
Expected: PASS (all tests in the file, including the 9 new ones and every pre-existing test — `_authenticate`/`_request_and_print_code` are new functions not yet called from `_run()`, so nothing existing changes behavior).

- [ ] **Step 5: Commit**

```bash
git add src/mycelium/node/cli.py tests/node/test_cli.py
git commit -m "feat: add mycelium-node device-flow polling orchestrator (issue #34)"
```

---

### Task 3: Wire `_authenticate` into `_run()`, cache the token, update docs

**Files:**
- Modify: `src/mycelium/node/cli.py`
- Modify: `tests/node/test_cli.py`
- Modify: `docs/OPERATIONS.md`

**Interfaces:**
- Consumes (from Task 2): `_authenticate(client, sleep) -> str`.
- Produces: `_run()` gains three new parameters — `default_github_token_path: Path = DEFAULT_GITHUB_TOKEN_PATH`, `device_flow_client=github_device_flow`, `device_flow_sleep=asyncio.sleep` — all defaulted so `main()`'s existing `asyncio.run(_run(args, process))` call site is unaffected. New module constants `DEFAULT_GITHUB_TOKEN_PATH: Path` and `_STALE_GITHUB_TOKEN_REASON: str`.

- [ ] **Step 1: Write the failing tests**

First, replace the now-obsolete test. In `tests/node/test_cli.py`, find:

```python
async def test_run_omits_github_token_when_no_github_token_file_given(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")

    received = {}
    registered_event = asyncio.Event()

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))
        registered_event.set()
        await websocket.wait_closed()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(_run(args, process))
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert "github_token" not in received
```

This test's premise — "no `--github-token-file` given means no GitHub token is sent" — is exactly what this ticket changes (that case now runs the device flow / reads the default cache path instead of silently omitting the token). Replace it with:

```python
async def test_run_reads_cached_token_from_default_path_when_flag_omitted(
    tmp_path, monkeypatch, fake_vllm_server
):
    """No --github-token-file given, but a token already exists at the
    default cache path (injected via default_github_token_path for
    testability, since the real default is ~/.mycelium/github-token) —
    must be read verbatim, and the device-flow client must never be
    called."""
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    default_token_path = tmp_path / "cached-github-token"
    default_token_path.write_text("gh-cached-token\n")

    class _ExplodingClient:
        def request_device_code(self):
            raise AssertionError("device flow must not run when a cached token exists")

        def poll_once(self, device_code, interval):
            raise AssertionError("device flow must not run when a cached token exists")

    received = {}
    registered_event = asyncio.Event()

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))
        registered_event.set()
        await websocket.wait_closed()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(
                args, process,
                default_github_token_path=default_token_path,
                device_flow_client=_ExplodingClient(),
            )
        )
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert received["github_token"] == "gh-cached-token"


async def test_run_authenticates_and_caches_token_when_default_path_missing(
    tmp_path, monkeypatch, fake_vllm_server
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    default_token_path = tmp_path / "not-yet-created" / "github-token"

    device = github_device_flow.DeviceCode(
        device_code="devcode123", user_code="ABCD-1234",
        verification_uri="https://github.com/login/device",
        expires_in=900, interval=5,
    )
    client = _FakeDeviceFlowClient(
        device, [github_device_flow.PollResult(token="gho_fresh", interval=5)]
    )

    received = {}
    registered_event = asyncio.Event()

    async def fake_coordinator(websocket):
        received.update(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "registered"}))
        registered_event.set()
        await websocket.wait_closed()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(fake_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--node-id", "test-node",
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(
                args, process,
                default_github_token_path=default_token_path,
                device_flow_client=client,
                device_flow_sleep=_no_op_sleep,
            )
        )
        await asyncio.wait_for(registered_event.wait(), timeout=5.0)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    assert received["github_token"] == "gho_fresh"
    assert default_token_path.read_text() == "gho_fresh"
    assert oct(default_token_path.stat().st_mode)[-3:] == "600"


async def test_run_rejects_missing_explicit_github_token_file_before_starting_vllm(
    tmp_path, monkeypatch, fake_vllm_server
):
    """An explicitly-passed --github-token-file that doesn't exist is a
    hard error — never a fallback into the interactive device flow (see
    the design doc for issue #34)."""
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text("placeholder")
    missing_token_file = tmp_path / "does-not-exist"

    args = parse_args(
        [
            "--coordinator-url", "wss://127.0.0.1:1",
            "--coordinator-cert", str(cert_path),
            "--github-token-file", str(missing_token_file),
            "--vllm-port", str(vllm_port),
        ]
    )
    process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)

    with pytest.raises(SystemExit, match="does not exist"):
        await _run(args, process)

    # vLLM must never have been started — the check happens before
    # process.start(), so there's nothing to clean up here and no
    # subprocess was spawned.


async def test_run_prints_stale_token_hint_on_matching_rejection_reason(
    tmp_path, monkeypatch, fake_vllm_server, capsys
):
    vllm_port = fake_vllm_server.server_address[1]
    monkeypatch.setattr(
        vllm_process, "build_command", lambda model, port_: [sys.executable, "-c", "import time; time.sleep(600)"]
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certs.ensure_cert(cert_path, key_path, "127.0.0.1")
    default_token_path = tmp_path / "github-token"
    default_token_path.write_text("stale-token")

    async def rejecting_coordinator(websocket):
        await websocket.recv()
        await websocket.send(json.dumps(
            {"type": "registration_rejected", "reason": "invalid or expired GitHub token"}
        ))
        await websocket.close()

    server_ctx = _server_ssl_context(cert_path, key_path)
    async with websockets.serve(rejecting_coordinator, "127.0.0.1", 0, ssl=server_ctx) as coordinator:
        coord_port = coordinator.sockets[0].getsockname()[1]
        args = parse_args(
            [
                "--coordinator-url", f"wss://127.0.0.1:{coord_port}",
                "--coordinator-cert", str(cert_path),
                "--vllm-port", str(vllm_port),
                "--node-key-file", str(tmp_path / "node-key.pem"),
            ]
        )
        process = vllm_process.VLLMProcess(model=args.model, gpu=args.gpu, port=args.vllm_port)
        run_task = asyncio.create_task(
            _run(args, process, default_github_token_path=default_token_path)
        )
        await asyncio.sleep(1.5)  # let it attempt once and get rejected
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    out = capsys.readouterr().out
    assert "stale GitHub token" in out
    assert str(default_token_path) in out
```

Also update `test_run_rejects_empty_github_token_file_before_starting_vllm` (the existing test right above the one you're adding) — no change needed to its body, since an explicitly-passed, existing-but-empty file still hits the same `is not None` branch as before. Leave it as-is.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/node/test_cli.py -v`
Expected: FAIL — the four new/replaced tests fail with `TypeError: _run() got an unexpected keyword argument 'default_github_token_path'` (or similar for `device_flow_client`/`device_flow_sleep`), since `_run()` doesn't accept these parameters yet.

- [ ] **Step 3: Implement**

No import-block changes needed here — Task 2 already added `webbrowser` and `github_device_flow` to `src/mycelium/node/cli.py`'s imports.

Add two module constants right after the imports (before `def parse_args`):

```python
DEFAULT_GITHUB_TOKEN_PATH = Path.home() / ".mycelium" / "github-token"

# Must match the exact rejection reason string server.py's
# _handle_registration sends for github_identity.InvalidGithubToken (see
# coordinator/server.py) — a single, deliberate string-match coupling
# used only to print a more useful diagnostic hint on retry, not to
# change control flow. See the design doc for issue #34.
_STALE_GITHUB_TOKEN_REASON = "invalid or expired GitHub token"
```

Change `_run()`'s signature and the token-resolution block. Replace:

```python
async def _run(args: argparse.Namespace, process: VLLMProcess) -> None:
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
            github_token = args.github_token_file.read_text().strip()
            if not github_token:
                raise SystemExit(f"--github-token-file at {args.github_token_file} is empty")
```

with:

```python
async def _run(
    args: argparse.Namespace,
    process: VLLMProcess,
    default_github_token_path: Path = DEFAULT_GITHUB_TOKEN_PATH,
    device_flow_client=github_device_flow,
    device_flow_sleep=asyncio.sleep,
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
            github_token = await _authenticate(device_flow_client, device_flow_sleep)
            default_github_token_path.parent.mkdir(parents=True, exist_ok=True)
            default_github_token_path.write_text(github_token)
            default_github_token_path.chmod(0o600)
```

Finally, update the registration-rejected handler. Replace:

```python
            except registration.RegistrationError as exc:
                delay = next(registration_backoff)
                print(f"registration failed: {exc}; retrying in {delay:.1f}s", flush=True)
                await websocket.close()
                await asyncio.sleep(delay)
                continue
```

with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/node/test_cli.py -v`
Expected: PASS (every test in the file).

Then run the full suite to confirm nothing else broke:

Run: `pytest -v`
Expected: PASS (every test in the repo, including Tasks 1 and 2's new tests).

- [ ] **Step 5: Update `docs/OPERATIONS.md`**

Replace lines 119–142 (the `**Get a GitHub token for the node's first registration.**` paragraph through the `mycelium-node \ ...` code block that follows it) with:

```markdown
**GitHub sign-in for the node's first registration.** The first time this
node's keypair registers, `mycelium-node` drives GitHub's OAuth device
flow itself — no browser or open inbound port needed on the node:

```bash
mycelium-node \
  --coordinator-url wss://<coordinator-ip>:8765 \
  --coordinator-cert ~/.mycelium/coordinator-cert.pem
```

```
First copy your one-time code: ABCD-1234
Then visit: https://github.com/login/device and enter it
```

Copy the code, open that URL on any device with a browser (not
necessarily the node itself), and authorize it. `mycelium-node` polls in
the background and continues automatically once you do — no restart, no
extra flag. The resulting token is cached to `~/.mycelium/github-token`
(`chmod 600`), so this only happens once per node; every later run (or
reconnect) reuses the cached token silently. Override the cache location
with `--github-token-file`.

Prefer to supply a token yourself instead of the interactive flow — e.g.
`gh auth token` if you have the GitHub CLI authenticated, or a Personal
Access Token from `github.com/settings/tokens` (no scopes are required,
since only `GET /user` is called)? Save it to that same path yourself
before starting the node, or point `--github-token-file` at wherever you
saved it:

```bash
echo "<your-github-token>" > ~/.mycelium/github-token
chmod 600 ~/.mycelium/github-token
```

Either way, this is only needed the **first** time this node's keypair
registers — the coordinator remembers the binding for as long as it keeps
running (see the limitation below), and a reconnecting node with an
already-known public key is accepted without it.
```

Then, in the "What happens" numbered list a little further down, replace item 3:

```markdown
3. Generates (on first run only — persisted afterward) an Ed25519
   keypair at `~/.mycelium/node-key.pem` by default, override with
   `--node-key-file`. Sends a registration message (model + node ID +
   public key + a signature proving it holds the matching private key,
   plus the GitHub token from `--github-token-file` if this is the
   key's first registration) and waits for the coordinator to ack it.
```

with:

```markdown
3. Generates (on first run only — persisted afterward) an Ed25519
   keypair at `~/.mycelium/node-key.pem` by default, override with
   `--node-key-file`. If this is the key's first registration, obtains a
   GitHub token — via the cached/hand-supplied `--github-token-file` if
   one exists, otherwise the interactive device flow above (cached to
   that same path afterward). Sends a registration message (model + node
   ID + public key + a signature proving it holds the matching private
   key, plus that GitHub token) and waits for the coordinator to ack it.
```

Finally, replace the `**A coordinator restart forgets every node's GitHub binding**` paragraph (the one ending in `...for real deployments.`) with:

```markdown
**A coordinator restart forgets every node's GitHub binding** (issue
#39) — bindings are in-memory only, exactly like the rest of the
coordinator's registry. Combined with GitHub OAuth's default 8-hour
access-token expiry, a volunteer's cached `~/.mycelium/github-token` is
quite likely already expired by the time any coordinator restart
happens, so a restart can force a fresh GitHub sign-in — `mycelium-node`
retries registration with backoff but never auto-detects or auto-clears
a stale cached token; delete `~/.mycelium/github-token` yourself to
force the device flow to run again. Whoever registers Mycelium's GitHub
App should turn *off* "Expire user access tokens" in that app's settings
to avoid this for real deployments.

**`mycelium-node`'s GitHub App `client_id` ships as a placeholder** until
the operator registers a real GitHub App (device flow enabled, "Expire
user access tokens" off) and swaps `CLIENT_ID` in
`src/mycelium/node/github_device_flow.py`. Running the interactive
device flow before that swap fails immediately with `error:
mycelium-node's GitHub App is not configured yet` — the manual
`--github-token-file` path above works regardless, since it never talks
to the device-flow endpoints at all.
```

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/node/cli.py tests/node/test_cli.py docs/OPERATIONS.md
git commit -m "feat: mycelium-node drops the manual-only GitHub token path, adds device-flow sign-in (issue #34)"
```
