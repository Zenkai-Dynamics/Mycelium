"""Repo-wide test safety nets."""

import webbrowser

import pytest


@pytest.fixture(autouse=True)
def _fail_on_real_webbrowser_open(monkeypatch):
    """Any test that reaches a real webbrowser.open() call without
    explicitly injecting a fake open_browser fails loudly instead of
    opening a real browser tab. See the design doc / incident history for
    issue #34 — this exact failure mode already happened once."""

    def _fail(url, *args, **kwargs):
        pytest.fail(f"real webbrowser.open({url!r}) called — inject a fake open_browser instead")

    monkeypatch.setattr(webbrowser, "open", _fail)
