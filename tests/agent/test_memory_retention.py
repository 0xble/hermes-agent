"""``MemoryManager.sync_all`` filters every turn before any provider retains it.

A catalog provider's ``sync_turn`` takes only ``(user, assistant, session_id)``, so the manager is
the one place that still knows a turn's provenance and can keep generated turns out of memory.
"""

import pytest

from agent.memory_manager import MemoryManager
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE
from tools.process_registry_notifications import format_process_notification

ASSISTANT_RESULT = "The Slack attribution fix passed 1079 tests."


class _TextOnlyProvider:
    """The catalog Hindsight plugin's sync_turn signature: no provenance kwargs."""

    name = "text-only"

    def __init__(self):
        self.turns = []

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        self.turns.append((user_content, assistant_content))


def _sync(user, assistant, **provenance):
    manager = MemoryManager()
    provider = _TextOnlyProvider()
    manager._providers = [provider]  # type: ignore[list-item]  # duck-typed provider
    manager.sync_all(user, assistant, session_id="s-1", **provenance)
    assert manager.flush_pending(timeout=5) is True
    return provider.turns


def _notice():
    return format_process_notification({
        "type": "completion", "session_id": "process-1", "exit_code": 0,
        "command": "run tests", "output": "1079 passed",
    })


@pytest.mark.parametrize("user", [
    _notice(),
    CONTINUATION_PROMPT_TEMPLATE.format(goal="Verify the attribution change"),
])
def test_generated_turn_never_reaches_a_text_only_provider(user):
    assert _sync(user, ASSISTANT_RESULT) == []


@pytest.mark.parametrize("provenance", [
    {"display_kind": "internal_notification"},
    {"platform": "cron"},
])
def test_runtime_provenance_keeps_the_turn_out_of_memory(provenance):
    assert _sync("Runtime-generated work", ASSISTANT_RESULT, **provenance) == []


def test_human_suffix_after_a_generated_notice_is_retained_alone():
    request = "Remember the decision to use the existing publisher."
    assert _sync(_notice() + "\n\n" + request, ASSISTANT_RESULT) == [(request, ASSISTANT_RESULT)]


def test_status_only_reply_and_recalled_context_are_stripped():
    assert _sync("What did the tests show?", "Done.") == [("What did the tests show?", "")]
    assert _sync("<memory-context>Injected recalled material</memory-context>", ASSISTANT_RESULT) == [
        ("", ASSISTANT_RESULT),
    ]
