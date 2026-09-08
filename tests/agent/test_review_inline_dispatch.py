"""The model-facing review tool must reach native dispatch, not its registry stub."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext


def test_review_tool_dispatches_with_parent_and_candidate():
    parent = SimpleNamespace()
    messages = [{"role": "user", "content": "Review the accepted change"}]
    args = {"repository": "/candidate", "base_revision": "HEAD", "accepted_scope": ["a.py"]}
    candidate = object()
    with patch("agent.review_candidate.capture_review_candidate", return_value=candidate) as capture, patch(
        "agent.review_engine.start_review", return_value={"status": "dispatched"}
    ) as dispatch:
        result = INLINE_TOOL_EXECUTORS["review_current_work"](
            parent, args, InlineToolContext("parent-task", messages=messages)
        )
    assert result == {"status": "dispatched"}
    assert parent._review_yield_requested is True
    assert capture.call_args.kwargs["accepted_scope"] == ["a.py"]
    assert dispatch.call_args.kwargs["candidate"] is candidate
    assert dispatch.call_args.kwargs["parent_agent"] is parent
    assert dispatch.call_args.kwargs["messages"] is messages


def test_review_tool_is_parent_only():
    child = SimpleNamespace(is_subagent=True, _delegate_depth=1)
    with pytest.raises(ValueError, match="parent-only"):
        INLINE_TOOL_EXECUTORS["review_current_work"](
            child,
            {"repository": "/candidate", "base_revision": "HEAD", "accepted_scope": ["a.py"]},
            InlineToolContext("child-task", messages=[]),
        )
