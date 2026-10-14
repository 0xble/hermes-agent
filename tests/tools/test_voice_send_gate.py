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


def _agent(db, session_id="sid"):
    return SimpleNamespace(session_id=session_id, _session_db=db)


def test_classifier_shell_forms_and_read_only(tmp_path):
    commands = [
        "slack message send --text hi",
        "sh -c 'slack message send --text hi'",
        'bash -lc "slack message send --text hi"',
        "zsh -c 'slack message send --text hi'",
        "env FOO=1 slack message send --text hi",
        "nohup slack message send --text hi",
        "/abs/path/slack message send --text hi",
        "echo before && slack message send --text hi",
        "printf x | slack message send --text hi",
        "( slack message send --text hi )",
        "imsg send hi",
        "gog gmail send --to x",
        "gog gmail reply --to x",
        "gog gmail drafts send --to x",
        "wacli send hi",
        "quo message send hi",
        "telegram send chat hi",
    ]
    for command in commands:
        assert voice_send_gate.classify_voice_send("terminal", {"command": command}), command

    for command in (
        "slack message list",
        "slack message send --help",
        "gog gmail draft create",
        "gog gmail drafts list",
        "quo message list",
        "telegram chats",
    ):
        assert voice_send_gate.classify_voice_send("terminal", {"command": command}) is None, command


def test_classifier_scans_only_executed_script_files(tmp_path, monkeypatch):
    read_files = {
        "jobs.json": '{"message": "slack message send"}',
        "voice_send_gate.py": "slack message send",
        "SKILL.md": "slack message send",
        "test_voice_send_gate.py": "slack message send",
        "file.py": "slack message send",
    }
    paths = {name: tmp_path / name for name in read_files}
    for name, content in read_files.items():
        paths[name].write_text(content)
    monkeypatch.chdir(tmp_path)

    for command in (
        f"cat {paths['jobs.json']}",
        f"git diff {paths['voice_send_gate.py']}",
        f"grep -n x {paths['SKILL.md']}",
        f"sed -n 1,40p {paths['test_voice_send_gate.py']}",
        f"pytest {paths['test_voice_send_gate.py']}",
        f"python3 -m pytest {paths['file.py']}",
    ):
        assert voice_send_gate.classify_voice_send("terminal", {"command": command}) is None, command


def test_classifier_scans_executed_script_files(tmp_path, monkeypatch):
    shell_script = tmp_path / "send.sh"
    shell_script.write_text("slack --account lpg message send --text hi")
    python_script = tmp_path / "send.py"
    python_script.write_text("subprocess.run(['telegram', 'send', 'chat', 'hi'])")
    monkeypatch.chdir(tmp_path)

    for command in (
        "bash ./send.sh",
        "./send.sh",
        "python3 send.py",
        "python3.12 send.py",
        "env A=1 ./send.sh",
    ):
        found = voice_send_gate.classify_voice_send("terminal", {"command": command})
        assert found and found["action_hash"], command


def test_native_send_tools_are_classified():
    found = voice_send_gate.classify_voice_send("mcp__telegram__send", {"chat_id": "1", "text": "hi"})
    assert found and found["cli"] == "mcp__telegram__send"


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


def test_shared_dispatch_choke_point_blocks_execute_code_style_call(monkeypatch):
    import model_tools

    dispatched = []
    monkeypatch.setattr(model_tools, "_execute_tool", lambda *args, **kwargs: dispatched.append(1))
    result = model_tools.handle_function_call("terminal", {"command": "slack message send C1 hi"})
    assert "BLOCKED: approval required before sending a message in Brian" in result
    assert not dispatched


def test_shared_dispatch_choke_point_runs_voice_gate_once(monkeypatch):
    import model_tools

    calls = []
    monkeypatch.setattr(voice_send_gate, "authorize_voice_send", lambda *args: calls.append(args) or "blocked")
    result = model_tools.handle_function_call("terminal", {"command": "slack message send C1 hi"})
    assert "blocked" in result
    assert len(calls) == 1


def test_goal_judge_voice_send_floor_is_scoped_to_current_turn(monkeypatch):
    from hermes_cli import goals

    monkeypatch.setattr(goals, "_call_goal_judge_llm", lambda *a: '{"verdict":"done","reason":"ok"}')
    older = [{
        "timestamp": 10.0,
        "tool": "terminal",
        "call": json.dumps({"command": "slack message send earlier"}),
        "output": "sent",
    }]
    current = [{
        "timestamp": 20.0,
        "tool": "terminal",
        "call": json.dumps({"command": "slack message send now"}),
        "output": "sent",
    }]

    verdict, reason, *_ = goals.judge_goal("send", "done", evidence=older, voice_send_since=20.0)
    assert (verdict, reason) == ("done", "ok")

    verdict, reason, *_ = goals.judge_goal("send", "done", evidence=current, voice_send_since=20.0)
    assert verdict == "blocked"
    assert "deterministic voice-send floor" in reason

    # Direct callers retain the incident-replay behavior of checking the whole window.
    verdict, reason, *_ = goals.judge_goal("send", "done", evidence=older)
    assert verdict == "blocked"
    assert "deterministic voice-send floor" in reason


def test_goal_judge_ignores_gate_refusal_but_blocks_unapproved_send(monkeypatch):
    from hermes_cli import goals

    called = []
    monkeypatch.setattr(goals, "_call_goal_judge_llm", lambda *a: called.append(1) or '{"verdict":"done","reason":"ok"}')
    refused = [{"tool": "terminal", "call": json.dumps({"command": "slack message send hi"}),
                "output": "BLOCKED: approval required before sending a message in Brian's voice (not approved; action=abc)"}]
    verdict, reason, *_ = goals.judge_goal("send", "done", evidence=refused)
    assert verdict == "done"
    assert reason == "ok"
    assert called == [1]

    called.clear()
    sent = [{"tool": "terminal", "call": json.dumps({"command": "slack message send hi"}), "output": "sent"}]
    verdict, reason, *_ = goals.judge_goal("send", "done", evidence=sent)
    assert verdict == "blocked"
    assert "deterministic voice-send floor" in reason
    assert not called
