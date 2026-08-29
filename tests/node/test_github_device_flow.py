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
