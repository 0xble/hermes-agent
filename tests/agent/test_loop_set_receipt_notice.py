"""Generalized committed mutation receipts for loop_set."""

import json
from unittest.mock import patch

from agent import inline_tool_executors as ite


class _Agent:
    def __init__(self):
        self.notice_callback = lambda notice: None
        self.notices = []
        self.printed = []

    def _emit_notice(self, notice):
        self.notices.append(notice)

    def _vprint(self, text, force=False):
        self.printed.append(text)


def _emit(agent, result):
    ite.emit_terminal_post_tool_call(
        agent, function_name="loop_set", function_args={}, result=result,
        effective_task_id="t", tool_call_id="c",
    )


def test_loop_receipt_uses_loop_gate_and_distinct_key():
    result = json.dumps({"success": True, "persisted": True, "notice": "↻ Loop revised: slower"})
    with patch("hermes_cli.config.load_config_readonly", return_value={"loops": {"auto_notices": True}}), \
         patch("model_tools._emit_post_tool_call_hook"):
        agent = _Agent()
        _emit(agent, result)
    assert [n.text for n in agent.notices] == ["↻ Loop revised: slower"]
    assert agent.notices[0].key == agent.notices[0].id == "loop.receipt"


def test_loop_receipt_gate_off_and_unpersisted_are_silent():
    with patch("hermes_cli.config.load_config_readonly", return_value={"loops": {"auto_notices": False}}), \
         patch("model_tools._emit_post_tool_call_hook"):
        agent = _Agent()
        _emit(agent, json.dumps({"success": True, "persisted": True, "notice": "x"}))
        assert not agent.notices
    with patch("hermes_cli.config.load_config_readonly", return_value={"loops": {}}), \
         patch("model_tools._emit_post_tool_call_hook"):
        agent = _Agent()
        _emit(agent, json.dumps({"success": True, "persisted": False, "notice": "x"}))
        assert not agent.notices
