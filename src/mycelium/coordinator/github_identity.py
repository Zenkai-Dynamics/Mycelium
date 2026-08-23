"""Real GitHub identity verification for node registration.

Called by NodeRegistry.resolve_identity (see coordinator/registry.py) the
first time a node's public key registers — never on a reconnect. See the
design doc for issue #39 for the full rationale, including why a real
401/403 from GitHub (the volunteer's problem) gets a different rejection
reason than every other failure mode (the coordinator's/network's
problem). Tests never call this module's real verify_identity — they
inject a fake identity_verifier into NodeRegistry instead, so no test
ever makes a real network call to GitHub.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

GITHUB_USER_URL = "https://api.github.com/user"

# Bounds the blocking GET /user call — see the design doc for issue #39.
# This is also why registration.REGISTRATION_TIMEOUT_SECONDS and
# server.FIRST_MESSAGE_TIMEOUT_SECONDS were bumped to 15.0: comfortable
# headroom over this call's worst case plus existing overhead.
VERIFY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class GithubIdentity:
    # GitHub's stable numeric user id (as a string) — immutable across
    # username renames. This, not login, is what #35's cap and #37's ban
    # will index on.
    id: str
    # The GitHub username at verification time — display-only (see
    # NodeRegistry.list_nodes), can go stale if the volunteer renames
    # later. A cosmetic concern only, since nothing keys off it.
    login: str


class IdentityVerificationError(Exception):
    """Base class: verify_identity failed for any reason."""


class InvalidGithubToken(IdentityVerificationError):
    """GitHub itself rejected the token (401/403) — the volunteer's
    problem, not the coordinator's."""


class GithubUnreachable(IdentityVerificationError):
    """Timeout, network error, or an unexpected status/response shape —
    the coordinator's/network's problem, not the token's. Deliberately
    distinct from InvalidGithubToken so a transient GitHub outage isn't
    reported to a volunteer as "your token is bad"."""


def _fetch_user(github_token: str) -> GithubIdentity:
    """Blocking GET /user call — only ever run via asyncio.to_thread,
    never called directly from an async context."""
    request = urllib.request.Request(
        GITHUB_USER_URL,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "mycelium-coordinator",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=VERIFY_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise InvalidGithubToken(f"GitHub rejected the token: HTTP {exc.code}") from exc
        raise GithubUnreachable(f"unexpected GitHub response: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GithubUnreachable(f"could not reach GitHub: {exc}") from exc

    try:
        return GithubIdentity(id=str(body["id"]), login=body["login"])
    except (KeyError, TypeError) as exc:
        raise GithubUnreachable(f"unexpected GitHub response shape: {body!r}") from exc


async def verify_identity(github_token: str) -> GithubIdentity:
    """Verify github_token against GitHub's GET /user and return the
    resulting identity. Raises InvalidGithubToken for a real 401/403,
    GithubUnreachable for anything else. Runs the blocking call in a
    thread so it never blocks the coordinator's event loop — the same
    pattern node/cli.py uses for vLLM's start/wait_ready."""
    return await asyncio.to_thread(_fetch_user, github_token)
