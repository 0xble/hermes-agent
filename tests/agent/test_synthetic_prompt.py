"""The shared synthetic-prompt classifier drives both memory gates: auto-recall and retention.

Behavior is asserted through the real producers' formatters (process and delegation notices, goal
continuations, the cron preamble) and the real call sites: the turn-start prefetch in
``build_turn_context`` and the post-turn ``_sync_external_memory_for_turn`` path.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.memory_manager import MemoryManager
from agent.synthetic_prompt import auto_recall_query, human_prompt_text
from agent.turn_context import _memory_turn_start_and_prefetch
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE
from plugins.memory.hindsight.retention import filter_retain_messages
from tools.delegation_resume import AUTO_RESUME_NOTICE_OPEN
from tools.process_registry_notifications import PROCESS_NOTIFICATION_END, format_process_notification

HUMAN = "What did we decide about the deploy pipeline?"


def _process_notice() -> str:
    from gateway.run_notifications import _mark_internal_notification

    evt = {"type": "completion", "session_id": "proc_1", "command": "./bin/ci gate", "exit_code": 0,
           "output": "all checks passed"}
    return _mark_internal_notification(format_process_notification(evt))


def _delegation_notice() -> str:
    evt = {"type": "async_delegation", "delegation_id": "deleg_1", "status": "completed",
           "goal": "Review the release notes", "result": "No findings."}
    return format_process_notification(evt)


def _goal_prompt() -> str:
    return CONTINUATION_PROMPT_TEMPLATE.format(goal="Ship the deploy pipeline change")


def _goal_prompt_after_wait() -> str:
    from hermes_cli.goals import GOAL_WAIT_LIFTED_NOTE_OPEN

    return _goal_prompt() + "\n\n" + GOAL_WAIT_LIFTED_NOTE_OPEN + "pid 4242 has exited. Verify its result before continuing.]"


def _cron_prompt() -> str:
    from cron.scheduler_prompt import _CRON_HINT

    return _CRON_HINT + "\n\nSummarize overnight alerts."


def _resume_notice() -> str:
    return AUTO_RESUME_NOTICE_OPEN + f"is eligible for conservative recovery.\n{PROCESS_NOTIFICATION_END}"


# (prompt, display_kind, platform) as each producer delivers it to the agent today.
SYNTHETIC_TURNS = {
    "gateway process notice": (_process_notice, "internal_notification", "telegram"),
    "legacy unmarked process notice": (_process_notice, None, "telegram"),
    "delegation result": (_delegation_notice, "internal_notification", "telegram"),
    "legacy unmarked delegation result": (_delegation_notice, None, "telegram"),
    "goal continuation": (_goal_prompt, None, "telegram"),
    "goal continuation after a lifted wait": (_goal_prompt_after_wait, None, "telegram"),
    "cron run": (_cron_prompt, None, "cron"),
    "delegation recovery notice": (_resume_notice, "internal_notification", "telegram"),
    "unrecognized internal wake": (lambda: "Reply with exactly HERMES_READY 1234", "internal_notification", "telegram"),
    "tui crash continuation": (lambda: "Finish the migration", "auto_continue", "tui"),
}


@pytest.mark.parametrize("name", sorted(SYNTHETIC_TURNS))
def test_synthetic_turns_have_no_human_text_and_no_recall_query(name):
    build, display_kind, platform = SYNTHETIC_TURNS[name]
    prompt = build()

    assert human_prompt_text(prompt, display_kind=display_kind, platform=platform) is None
    assert auto_recall_query(prompt, display_kind=display_kind, platform=platform) == ""
    # The policy switch restores recall on the generated text itself.
    assert auto_recall_query(prompt, display_kind=display_kind, platform=platform, include_synthetic=True) == prompt.strip()


@pytest.mark.parametrize("display_kind,platform", [
    (None, "telegram"),  # an ordinary chat message
    ("steer", "telegram"),  # a mid-turn steer the person typed
    (None, "tui"),
])
def test_human_turns_recall_unchanged(display_kind, platform):
    assert auto_recall_query(HUMAN, display_kind=display_kind, platform=platform) == HUMAN


def test_relay_carrying_a_user_request_still_recalls():
    """Relays reach a session as ordinary inbound messages: no runtime display kind, no generated
    formatter. They stay human-authored for memory."""
    relay = "[relay from=hermes:default/20261005_160953_9bbfd080 receipt=abc] " + HUMAN

    assert auto_recall_query(relay, display_kind=None, platform="telegram") == relay


def test_user_text_that_merely_looks_generated_is_not_dropped():
    lookalike = "[IMPORTANT: remember that the staging deploy needs a manual approval]"

    assert auto_recall_query(lookalike, display_kind=None, platform="telegram") == lookalike


@pytest.mark.parametrize("build", [_process_notice, _goal_prompt, _goal_prompt_after_wait])
def test_human_suffix_merged_into_a_synthetic_turn_is_the_only_recall_query(build):
    merged = build() + "\n\n" + HUMAN

    assert auto_recall_query(merged, display_kind="internal_notification", platform="telegram") == HUMAN
    assert filter_retain_messages(merged, "Answer.", display_kind="internal_notification")[0] == HUMAN


def test_recovery_note_wrapping_a_generated_notice_is_fully_synthetic():
    note = "[System note: The previous turn was interrupted by a gateway restart.]\n\n"

    assert human_prompt_text(note + _delegation_notice()) is None
    assert human_prompt_text(note + _delegation_notice() + "\n\n" + HUMAN) == HUMAN


def test_retention_drops_structured_synthetic_turn_without_a_known_formatter():
    prompt = "Reply with exactly HERMES_READY 1234"

    assert filter_retain_messages(prompt, "HERMES_READY 1234", display_kind="internal_notification") == (
        None, "HERMES_READY 1234")
    assert filter_retain_messages(prompt, "HERMES_READY 1234") == (prompt, "HERMES_READY 1234")


def test_trivial_suffix_still_skips_recall():
    assert auto_recall_query(_goal_prompt() + "\n\nok", display_kind=None, platform="telegram") == ""


# -- call sites ---------------------------------------------------------------------------------


def _turn_agent(*, display_kind=None, platform="telegram", recall_synthetic_turns=False):
    manager = MagicMock()
    manager.prefetch_all.return_value = ""
    manager.recall_synthetic_turns = recall_synthetic_turns
    return SimpleNamespace(_memory_manager=manager, _user_turn_count=3, session_id="s-1",
                           _turn_display_kind=display_kind, platform=platform)


def test_turn_start_skips_recall_for_a_delegation_result_but_still_notifies_providers():
    agent = _turn_agent(display_kind="internal_notification")

    assert _memory_turn_start_and_prefetch(agent, _delegation_notice()) == ""
    agent._memory_manager.prefetch_all.assert_not_called()
    agent._memory_manager.on_turn_start.assert_called_once()


def test_turn_start_recalls_on_the_human_suffix_only():
    agent = _turn_agent(display_kind="internal_notification")

    _memory_turn_start_and_prefetch(agent, _process_notice() + "\n\n" + HUMAN)
    agent._memory_manager.prefetch_all.assert_called_once_with(HUMAN, session_id="s-1")


def test_turn_start_honors_the_recall_synthetic_turns_switch():
    agent = _turn_agent(recall_synthetic_turns=True)
    prompt = _goal_prompt()

    _memory_turn_start_and_prefetch(agent, prompt)
    agent._memory_manager.prefetch_all.assert_called_once_with(prompt.strip(), session_id="s-1")


def test_build_turn_context_stashes_the_turn_display_kind_for_memory():
    from tests.agent.test_turn_context import _FakeAgent, _build

    agent = _FakeAgent()
    agent._memory_manager = MagicMock(prefetch_all=MagicMock(return_value=""), recall_synthetic_turns=False)
    _build(agent, user_message=_delegation_notice(), persist_user_display_kind="internal_notification")
    assert agent._turn_display_kind == "internal_notification"
    agent._memory_manager.prefetch_all.assert_not_called()

    # A cached agent's next human turn must not inherit the previous internal kind.
    _build(agent, user_message=HUMAN)
    assert agent._turn_display_kind is None
    agent._memory_manager.prefetch_all.assert_called_once_with(HUMAN, session_id=agent.session_id)


def _post_turn_agent(*, display_kind, platform="telegram"):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = MagicMock(recall_synthetic_turns=False)
    agent.session_id = "s-1"
    agent.platform = platform
    agent._turn_display_kind = display_kind
    return agent


def test_post_turn_syncs_a_synthetic_turn_with_provenance_but_queues_no_recall():
    agent = _post_turn_agent(display_kind="internal_notification")

    agent._sync_external_memory_for_turn(original_user_message=_process_notice(),
                                         final_response="NO_REPLY", interrupted=False)

    kwargs = agent._memory_manager.sync_all.call_args.kwargs
    assert (kwargs["display_kind"], kwargs["platform"]) == ("internal_notification", "telegram")
    agent._memory_manager.queue_prefetch_all.assert_not_called()


def test_post_turn_queues_recall_for_a_human_turn():
    agent = _post_turn_agent(display_kind=None)

    agent._sync_external_memory_for_turn(original_user_message=HUMAN, final_response="We kept it.",
                                         interrupted=False)
    agent._memory_manager.queue_prefetch_all.assert_called_once_with(HUMAN, session_id="s-1")


def test_manager_forwards_provenance_only_to_providers_that_accept_it():
    class Legacy:
        name = "legacy"

        def __init__(self):
            self.calls = []

        def sync_turn(self, user_content, assistant_content, *, session_id=""):
            self.calls.append((user_content, session_id))

    class Aware(Legacy):
        name = "aware"

        def sync_turn(self, user_content, assistant_content, *, session_id="", display_kind=None, platform=None):
            self.calls.append((user_content, session_id, display_kind, platform))

    manager = MemoryManager()
    legacy, aware = Legacy(), Aware()
    manager._providers = [legacy, aware]  # type: ignore[list-item]  # duck-typed sync_turn signatures
    manager.sync_all("notice", "ok", session_id="s-1", display_kind="internal_notification", platform="cron")
    assert manager.flush_pending(timeout=5) is True

    assert legacy.calls == [("notice", "s-1")]
    assert aware.calls == [("notice", "s-1", "internal_notification", "cron")]


def test_hindsight_sync_turn_drops_a_structured_synthetic_turn(monkeypatch):
    from plugins.memory.hindsight import HindsightMemoryProvider

    provider = HindsightMemoryProvider()
    provider._mode, provider._auto_retain = "local_external", True
    built = []
    monkeypatch.setattr(provider, "_build_turn_messages",
                        lambda u, a, **kw: built.append((u, kw)) or [])

    provider.sync_turn("Reply with exactly HERMES_READY 1", "HERMES_READY 1", session_id="s-1",
                       display_kind="internal_notification", platform="telegram")
    assert built == [("Reply with exactly HERMES_READY 1",
                      {"display_kind": "internal_notification", "platform": "telegram"})]
    assert HindsightMemoryProvider._build_turn_messages(
        provider, "Reply with exactly HERMES_READY 1", "[SILENT]", display_kind="internal_notification") == []
