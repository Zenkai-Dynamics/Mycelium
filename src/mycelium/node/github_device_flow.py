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

# A sentinel, not the shipped default — kept as its own named constant
# (rather than inlined in the guard below) so tests can put CLIENT_ID into
# the "not configured" state explicitly, independent of whatever value
# actually ships. See Finding 5 of issue #34's final whole-branch review:
# a test that instead relied on the shipped CLIENT_ID literally being this
# placeholder would break the moment issue #48 swapped in a real value.
PLACEHOLDER_CLIENT_ID = "REPLACE_ME_WITH_REAL_GITHUB_APP_CLIENT_ID"

# Not secret — ships embedded in the mycelium-node package, the same way
# tools like the GitHub CLI embed their own device-flow client_id. This is
# the real, registered GitHub App's client_id (issue #48) — device flow
# enabled, "Expire user authorization tokens" off, "No access" permissions,
# installable only on the owning account. Verified live: device-flow
# authorization from an unrelated GitHub account succeeds, confirming the
# install-scope restriction doesn't block outside volunteers (see the
# design doc for issue #48).
CLIENT_ID = "Iv23li695Ty38NLrU0K3"

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
    if CLIENT_ID == PLACEHOLDER_CLIENT_ID:
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
