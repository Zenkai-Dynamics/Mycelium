"""Resolves a client's completion request into the messages array the
node wire carries.

See the design doc for issue #55, and the Phase 2 implementation design
doc for why the coordinator is the single normalization point: the node
then has exactly one shape to handle and builds no part of the
conversation itself.

Validation here is deliberately structural only — a non-empty list of
objects with a string role and string content. `role` is NOT checked
against a vocabulary: chat templates already use roles beyond
system/user/assistant ("tool" being the obvious one), and rejecting
those would make Mycelium the component that breaks a legitimate
request. Anything vLLM itself rejects comes back as a client-caused
fault (issue #58) rather than being pre-empted here.
"""

from __future__ import annotations


class InvalidCompletionRequest(Exception):
    """Raised when a completion request names neither prompt nor
    messages, names both, or carries a malformed messages array. The
    message text is client-facing — it is relayed verbatim as a
    complete_error reason."""


def normalize_messages(message: dict) -> list[dict]:
    """Return the messages array for `message`, expanding the one-message
    `prompt` shorthand if that is what was sent.

    Raises InvalidCompletionRequest, whose text is safe to relay to the
    client, if the request is ambiguous or malformed.
    """
    prompt = message.get("prompt")
    messages = message.get("messages")

    if prompt is not None and messages is not None:
        # Deliberately not resolved by a precedence rule — a caller who
        # sets both is confused and should be told, rather than having
        # one silently dropped. See the design doc for issue #55.
        raise InvalidCompletionRequest(
            "send either prompt or messages, not both"
        )
    if prompt is None and messages is None:
        raise InvalidCompletionRequest("either prompt or messages is required")

    if prompt is not None:
        if not isinstance(prompt, str) or not prompt:
            raise InvalidCompletionRequest("prompt must be a non-empty string")
        return [{"role": "user", "content": prompt}]

    if not isinstance(messages, list) or not messages:
        raise InvalidCompletionRequest("messages must be a non-empty list")
    for index, entry in enumerate(messages):
        if not isinstance(entry, dict):
            raise InvalidCompletionRequest(
                f"messages[{index}] must be an object with role and content"
            )
        role = entry.get("role")
        content = entry.get("content")
        if not isinstance(role, str) or not role:
            raise InvalidCompletionRequest(
                f"messages[{index}] needs a non-empty string role"
            )
        if not isinstance(content, str):
            raise InvalidCompletionRequest(
                f"messages[{index}] needs a string content"
            )
    return messages
