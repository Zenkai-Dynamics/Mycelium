"""Tests for mycelium.coordinator.completion_request."""

import pytest

from mycelium.coordinator.completion_request import (
    InvalidCompletionRequest,
    normalize_messages,
)


def test_prompt_becomes_a_single_user_message():
    assert normalize_messages({"prompt": "hello"}) == [{"role": "user", "content": "hello"}]


def test_messages_pass_through_unchanged():
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert normalize_messages({"messages": messages}) == messages


def test_both_prompt_and_messages_is_rejected_as_ambiguous():
    with pytest.raises(InvalidCompletionRequest) as exc:
        normalize_messages({"prompt": "hi", "messages": [{"role": "user", "content": "hi"}]})
    assert "both" in str(exc.value).lower()


def test_neither_prompt_nor_messages_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({})


def test_empty_messages_list_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"messages": []})


def test_empty_prompt_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"prompt": ""})


def test_message_that_is_not_an_object_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"messages": ["just a string"]})


def test_message_missing_content_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"messages": [{"role": "user"}]})


def test_message_with_non_string_content_is_rejected():
    with pytest.raises(InvalidCompletionRequest):
        normalize_messages({"messages": [{"role": "user", "content": 42}]})


def test_unfamiliar_role_is_accepted():
    """Role is not restricted to a vocabulary — chat templates already use
    'tool', and rejecting it would make Mycelium the component breaking a
    legitimate request. See the Phase 2 implementation design doc."""
    messages = [{"role": "tool", "content": "42"}]
    assert normalize_messages({"messages": messages}) == messages
