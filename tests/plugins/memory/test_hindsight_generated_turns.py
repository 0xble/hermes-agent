"""Generated-only turns cannot retain an orphan assistant process result."""

import pytest

from agent.prompt_builder import format_steer_marker
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE
from plugins.memory.hindsight import HindsightMemoryProvider
from agent.memory_retention import filter_retain_messages
from tools.process_registry_notifications import format_process_notification


ASSISTANT_RESULT = "The Slack attribution fix passed 1079 tests."


@pytest.mark.parametrize("event", [
    {"type": "completion", "session_id": "process-1", "exit_code": 0,
     "command": "run tests", "output": "1079 passed"},
    {"type": "async_delegation", "delegation_id": "child-1", "status": "completed",
     "goal": "Check attribution", "summary": "1079 passed"},
])
def test_real_generated_notice_drops_orphan_assistant_result(event):
    notice = format_process_notification(event)
    assert notice
    provider = HindsightMemoryProvider()
    assert filter_retain_messages(notice, ASSISTANT_RESULT) == (None, None)
    assert provider._build_turn_messages(notice, ASSISTANT_RESULT) == []


def test_generated_goal_drops_substantive_assistant_result():
    notice = CONTINUATION_PROMPT_TEMPLATE.format(goal="Verify the attribution change")
    assert filter_retain_messages(notice, ASSISTANT_RESULT) == (None, None)


@pytest.mark.parametrize("provenance", [
    {"display_kind": "internal_notification"},
    {"platform": "cron"},
])
def test_runtime_provenance_drops_assistant_result(provenance):
    assert filter_retain_messages("Runtime-generated work", ASSISTANT_RESULT, **provenance) == (
        None, None,
    )


@pytest.mark.parametrize("append", [
    lambda request: "\n\n" + request,
    format_steer_marker,
])
def test_generated_notice_with_human_suffix_preserves_the_real_turn(append):
    notice = format_process_notification({
        "type": "completion", "session_id": "process-1", "exit_code": 0,
        "command": "run tests", "output": "1079 passed",
    })
    request = "Remember the decision to use the existing publisher."
    assert filter_retain_messages(notice + append(request), ASSISTANT_RESULT) == (
        request, ASSISTANT_RESULT,
    )
    messages = HindsightMemoryProvider()._build_turn_messages(
        notice + append(request), ASSISTANT_RESULT,
    )
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"].endswith(request)
    assert messages[1]["content"].endswith(ASSISTANT_RESULT)


@pytest.mark.parametrize("user", [
    "What did the tests show?",
    "[IMPORTANT: Please review my proposal]",
    "[Continuing toward your standing goal]\nThis is my literal quotation.",
    "",
    "<memory-context>Injected recalled material</memory-context>",
])
def test_real_or_empty_user_turn_does_not_blanket_drop_assistant(user):
    expected_user = None if not user or user.startswith("<memory-context>") else user
    assert filter_retain_messages(user, ASSISTANT_RESULT) == (expected_user, ASSISTANT_RESULT)
