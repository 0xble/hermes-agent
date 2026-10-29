"""Fork patch: a committed ``goal_set`` mutation surfaces its receipt as an agent notice.

The goal-lifecycle plugin has no send path of its own, so the receipt rides on the tool result
and ``emit_terminal_post_tool_call`` publishes it when the tool result is committed (the gateway
sends it immediately; CLI/TUI drivers flush notices at turn end). Read-only,
failed, or unpersisted results and the ``goals.auto_notices`` off switch stay silent.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import inline_tool_executors as ite


class _Agent:
    def __init__(self, *, with_callback=True):
        self.notice_callback = (lambda n: None) if with_callback else None
        self.notices = []
        self.printed = []

    def _emit_notice(self, notice):
        self.notices.append(notice)

    def _vprint(self, text, force=False):
        self.printed.append(text)


def _emit(agent, name, result):
    ite.emit_terminal_post_tool_call(
        agent, function_name=name, function_args={}, result=result,
        effective_task_id="t", tool_call_id="c",
    )


def _receipt(**fields):
    return json.dumps({"success": True, "persisted": True, "notice": "⊙ Goal set (20-turn budget): ship it", **fields})


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"goals": {}})
    with patch("model_tools._emit_post_tool_call_hook"):
        yield


def test_committed_set_emits_notice_with_full_text():
    agent = _Agent()
    _emit(agent, "goal_set", _receipt())
    assert [n.text for n in agent.notices] == ["⊙ Goal set (20-turn budget): ship it"]
    notice = agent.notices[0]
    assert notice.level == "info"
    # Finite TTL and stable key so the receipt expires and is clearable rather than sticky.
    assert notice.kind == "ttl" and isinstance(notice.ttl_ms, int) and notice.ttl_ms > 0
    assert notice.key == notice.id == ite.GOAL_RECEIPT_NOTICE_KEY
    assert agent.printed == []


def test_notice_falls_back_to_vprint_without_callback():
    agent = _Agent(with_callback=False)
    _emit(agent, "goal_set", _receipt())
    assert agent.printed == ["⊙ Goal set (20-turn budget): ship it"]
    assert agent.notices == []


@pytest.mark.parametrize("result", [
    json.dumps({"success": True, "action": "status", "state": {}}),
    json.dumps({"success": False, "persisted": False, "error_code": "goal_persistence_failed", "notice": "x"}),
    json.dumps({"success": True, "persisted": True, "notice": "   "}),
    json.dumps({"success": True, "persisted": True}),
    "not json",
])
def test_non_committed_results_are_silent(result):
    agent = _Agent()
    _emit(agent, "goal_set", result)
    assert agent.notices == [] and agent.printed == []


def test_other_tools_are_ignored():
    agent = _Agent()
    _emit(agent, "memory", _receipt())
    assert agent.notices == [] and agent.printed == []


def test_auto_notices_off_suppresses(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"goals": {"auto_notices": False}})
    agent = _Agent()
    _emit(agent, "goal_set", _receipt())
    assert agent.notices == [] and agent.printed == []


def test_notice_failure_never_breaks_the_hook():
    agent = SimpleNamespace(notice_callback=lambda n: None, _emit_notice=None)
    _emit(agent, "goal_set", _receipt())  # _emit_notice is not callable; must not raise
