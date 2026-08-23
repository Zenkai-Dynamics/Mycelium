"""Tests for mycelium.coordinator.github_identity."""

import json
import urllib.error
import urllib.request

from mycelium.coordinator import github_identity


class _FakeResponse:
    """Stand-in for the context-manager urlopen() returns."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


async def test_verify_identity_returns_id_and_login_on_success(monkeypatch):
    def fake_urlopen(request, timeout):
        assert request.full_url == github_identity.GITHUB_USER_URL
        assert request.get_header("Authorization") == "Bearer good-token"
        return _FakeResponse({"id": 12345, "login": "octocat"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    identity = await github_identity.verify_identity("good-token")

    assert identity == github_identity.GithubIdentity(id="12345", login="octocat")


async def test_verify_identity_passes_the_configured_timeout(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        return _FakeResponse({"id": 1, "login": "x"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    await github_identity.verify_identity("token")

    assert captured["timeout"] == github_identity.VERIFY_TIMEOUT_SECONDS


async def test_verify_identity_raises_invalid_token_on_401(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(github_identity.GITHUB_USER_URL, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("bad-token")
        assert False, "expected InvalidGithubToken"
    except github_identity.InvalidGithubToken:
        pass


async def test_verify_identity_raises_invalid_token_on_403(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(github_identity.GITHUB_USER_URL, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("bad-token")
        assert False, "expected InvalidGithubToken"
    except github_identity.InvalidGithubToken:
        pass


async def test_verify_identity_raises_unreachable_on_server_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(github_identity.GITHUB_USER_URL, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("any-token")
        assert False, "expected GithubUnreachable"
    except github_identity.GithubUnreachable:
        pass


async def test_verify_identity_raises_unreachable_on_network_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("any-token")
        assert False, "expected GithubUnreachable"
    except github_identity.GithubUnreachable:
        pass


async def test_verify_identity_raises_unreachable_on_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("any-token")
        assert False, "expected GithubUnreachable"
    except github_identity.GithubUnreachable:
        pass


async def test_verify_identity_raises_unreachable_on_malformed_response(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"unexpected": "shape"})  # no id/login

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    try:
        await github_identity.verify_identity("any-token")
        assert False, "expected GithubUnreachable"
    except github_identity.GithubUnreachable:
        pass


async def test_invalid_github_token_and_github_unreachable_are_both_identity_verification_errors():
    assert issubclass(github_identity.InvalidGithubToken, github_identity.IdentityVerificationError)
    assert issubclass(github_identity.GithubUnreachable, github_identity.IdentityVerificationError)
