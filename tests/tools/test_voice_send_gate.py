import json
from types import SimpleNamespace

import pytest

from tools import voice_send_gate


class MetaDB:
    def __init__(self):
        self.data = {}

    def get_meta(self, key):
        return self.data.get(key)

    def set_meta(self, key, value):
        self.data[key] = value


def _agent(db):
    return SimpleNamespace(session_id="sid", _session_db=db)


def test_classifier_send_table_and_read_only(tmp_path):
    assert voice_send_gate.classify_voice_send("terminal", {"command": "slack message send --text hi"})
    assert voice_send_gate.classify_voice_send("terminal", {"command": "slack message list"}) is None
    assert voice_send_gate.classify_voice_send("terminal", {"command": "gog gmail draft create"}) is None
    assert voice_send_gate.classify_voice_send("terminal", {"command": "quo message list"}) is None
    script = tmp_path / "send.sh"
    script.write_text("slack --account lpg message send --text hi")
    found = voice_send_gate.classify_voice_send("terminal", {"command": f"bash {script}"})
    assert found and found["action_hash"]


def test_one_human_approval_consumes_exactly_one_send(monkeypatch):
    db = MetaDB()
    calls = []
    monkeypatch.setattr(voice_send_gate.approval, "_presence", lambda _cb: (None, True, False, False))
    def approve_once(*_a, **_kw):
        calls.append(1)
        return {"approved": len(calls) == 1}
    monkeypatch.setattr(voice_send_gate.approval, "_human_decision", approve_once)
    agent = _agent(db)
    command = {"command": "slack message send hi"}
    assert voice_send_gate.authorize_voice_send(agent, "terminal", command) is None
    classified = voice_send_gate.classify_voice_send("terminal", command)
    assert classified is not None
    assert voice_send_gate.has_consumed_grant(db, "sid", classified["action_hash"])
    # The consumed grant cannot authorize a replay; it must prompt again and deny.
    result = voice_send_gate.authorize_voice_send(agent, "terminal", command)
    assert result and "not approved" in result
    assert len(calls) == 2


def test_yolo_mode_off_allowlist_and_cron_do_not_bypass(monkeypatch):
    db = MetaDB()
    agent = _agent(db)
    monkeypatch.setattr(voice_send_gate.approval, "_presence", lambda _cb: (None, False, False, False))
    monkeypatch.setattr(voice_send_gate.approval, "_yolo_active", lambda: True)
    monkeypatch.setattr(voice_send_gate.approval.approval_context, "_get_approval_mode", lambda: "off")
    monkeypatch.setitem(voice_send_gate.approval._session_approved, "sid", {"voice_send:*"})
    result = voice_send_gate.authorize_voice_send(agent, "terminal", {"command": "slack message send hi"})
    assert result and "no human approval bridge" in result


def test_tool_executor_applies_floor_before_dispatch(monkeypatch):
    from agent.tool_executor import _ToolCallRef, _pre_tool_block

    db = MetaDB()
    agent = _agent(db)
    monkeypatch.setattr(voice_send_gate.approval, "_presence", lambda _cb: (None, False, False, False))
    ref = _ToolCallRef("terminal", {"command": "slack message send hi"}, "task", "call", [])
    block, final_args = _pre_tool_block(agent, ref)
    assert block and "voice" in block.lower()
    assert final_args == ref.args


def test_judge_floor_blocks_before_llm(monkeypatch):
    from hermes_cli import goals
    called = []
    monkeypatch.setattr(goals, "_call_goal_judge_llm", lambda *a: called.append(1))
    evidence = [{"tool": "terminal", "call": json.dumps({"command": "slack message send hi"}), "output": "ok"}]
    verdict, reason, *_ = goals.judge_goal("send", "done", evidence=evidence)
    assert verdict == "blocked"
    assert "deterministic voice-send floor" in reason
    assert not called


def test_judge_floor_calls_llm_when_granted(monkeypatch):
    from hermes_cli import goals
    monkeypatch.setattr(goals, "_call_goal_judge_llm", lambda *a: '{"verdict":"done","reason":"ok"}')
    evidence = [{"tool": "terminal", "call": json.dumps({"command": "slack message send hi"}), "output": "ok", "voice_send_granted": True}]
    verdict, reason, *_ = goals.judge_goal("send", "done", evidence=evidence)
    assert verdict == "done"
    assert reason == "ok"
