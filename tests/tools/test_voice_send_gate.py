import json
import sqlite3
import threading
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


class TransactionalMetaDB(MetaDB):
    """Small state_meta-shaped store exercising the transaction callback path."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
        self.lock = threading.Lock()

    def get_meta(self, key):
        with self.lock:
            row = self.conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute(
                "INSERT INTO state_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.conn.commit()

    def _execute_write(self, callback):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = callback(self.conn)
                self.conn.commit()
                return result
            except BaseException:
                self.conn.rollback()
                raise


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


def test_classifier_known_boolean_flags_do_not_consume_subcommands():
    commands = [
        'slack --account ALIAS --json message send CHANNEL "$(cat file)" --yes',
        'telegram --agent --yes send peer "$(cat file)" --topic 1 --silent',
        "gog --json gmail send",
        "wacli --json send text",
    ]
    for command in commands:
        assert voice_send_gate.classify_voice_send("terminal", {"command": command}), command

    # A value-taking option still consumes its value, and later positional
    # arguments/read-only verbs remain negative.
    for command in (
        "slack --account ALIAS message list",
        "gog --json gmail search reply",
        "telegram --yes chats send",
        "wacli --json list send",
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


def test_classifier_rejects_embedded_or_argument_verbs():
    for command in (
        "gog gmail search reply",
        "slack message list --filter reply",
        "echo slack message send",
    ):
        assert voice_send_gate.classify_voice_send("terminal", {"command": command}) is None, command


def test_classifier_scans_inline_interpreter_sources():
    assert voice_send_gate.classify_voice_send(
        "terminal", {"command": "python3 -c \"subprocess.run(['slack','message','send','C1','hi'])\""}
    )
    assert voice_send_gate.classify_voice_send(
        "terminal", {"command": "python3 -c \"print(1)\""}
    ) is None


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


def test_production_sessiondb_allows_two_fresh_identical_grants(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    calls = []
    monkeypatch.setattr(voice_send_gate.approval, "_presence", lambda _cb: (None, True, False, False))

    def approve_twice(*_a, **_kw):
        calls.append(1)
        return {"approved": len(calls) <= 2}

    monkeypatch.setattr(voice_send_gate.approval, "_human_decision", approve_twice)
    agent = _agent(db)
    command = {"command": "slack message send identical"}

    assert voice_send_gate.authorize_voice_send(agent, "terminal", command) is None
    assert voice_send_gate.authorize_voice_send(agent, "terminal", command) is None
    third = voice_send_gate.authorize_voice_send(agent, "terminal", command)

    classified = voice_send_gate.classify_voice_send("terminal", command)
    assert classified is not None
    assert third and "not approved" in third
    assert len(voice_send_gate.consumed_grant_timestamps(
        db, "sid", classified["action_hash"])) == 2
    assert len(calls) == 3


def test_concurrent_identical_authorize_calls_consume_one_grant(monkeypatch):
    db = TransactionalMetaDB()
    agent = _agent(db)
    command = {"command": "slack message send concurrently"}
    classified = voice_send_gate.classify_voice_send("terminal", command)
    assert classified is not None
    key = voice_send_gate._meta_key("sid", classified["action_hash"])
    db.set_meta(key, json.dumps({
        "action_hash": classified["action_hash"], "session_id": "sid",
        "approved_at": 1.0, "consumed_at": [],
    }))

    entered = threading.Barrier(2)
    read_calls = 0
    read_calls_lock = threading.Lock()
    original_read = voice_send_gate._read_grant

    def synchronized_read(store, session_id, action_hash):
        nonlocal read_calls
        result = original_read(store, session_id, action_hash)
        if session_id == "sid" and action_hash == classified["action_hash"]:
            with read_calls_lock:
                read_calls += 1
                wait_for_peers = read_calls <= 2
            if wait_for_peers:
                entered.wait(timeout=5)
        return result

    monkeypatch.setattr(voice_send_gate, "_read_grant", synchronized_read)
    results = []
    errors = []

    def invoke():
        try:
            results.append(voice_send_gate.authorize_voice_send(agent, "terminal", command))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert results.count(None) == 1
    monkeypatch.setattr(voice_send_gate, "_read_grant", original_read)
    assert len(voice_send_gate.consumed_grant_timestamps(
        db, "sid", classified["action_hash"])) == 1


def test_yolo_mode_off_allowlist_and_cron_do_not_bypass(monkeypatch):
    db = MetaDB()
    agent = _agent(db)
    monkeypatch.setattr(voice_send_gate.approval, "_presence", lambda _cb: (None, False, False, False))
    monkeypatch.setattr(voice_send_gate.approval, "_yolo_active", lambda: True)
    monkeypatch.setattr(voice_send_gate.approval.approval_context, "_get_approval_mode", lambda: "off")
    monkeypatch.setitem(voice_send_gate.approval._session_approved, "sid", {"voice_send:*"})
    result = voice_send_gate.authorize_voice_send(agent, "terminal", {"command": "slack message send hi"})
    assert result and "no human approval bridge" in result


def test_shared_dispatch_gate_hashes_plugin_modified_args(monkeypatch):
    import model_tools

    captured = []
    monkeypatch.setattr(
        "hermes_cli.plugins._dispatch_pre_tool_call_hooks",
        lambda *args, **kwargs: (None, {"command": "slack message send final"}),
    )
    monkeypatch.setattr(
        voice_send_gate, "authorize_voice_send",
        lambda agent, tool, args: captured.append(args.copy()) or "blocked",
    )
    ids = model_tools._CallIds(None, "sid", None, None, None)
    result = model_tools.handle_function_call("terminal", {"command": "slack message list"}, session_id="sid")
    assert "blocked" in result
    assert captured == [{"command": "slack message send final"}]
def test_shared_dispatch_choke_point_blocks_execute_code_style_call(monkeypatch):
    import model_tools

    dispatched = []
    monkeypatch.setattr(
        voice_send_gate, "authorize_voice_send",
        lambda *args, **kwargs: "BLOCKED: approval required before sending a message in Brian's voice",
    )
    result = model_tools.handle_function_call("terminal", {"command": "slack message send C1 hi"})
    assert "BLOCKED: approval required before sending a message in Brian" in result
    assert dispatched == []


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


def test_goal_judge_voice_send_floor_survives_display_cap(monkeypatch):
    from hermes_cli import goals

    send = {
        "tool": "terminal",
        "call": json.dumps({"command": "slack message send unapproved"}),
        "output": "sent",
        "timestamp": 20.0,
        "voice_send_action_hash": "unapproved-hash",
        "voice_send_granted": False,
    }
    later = [
        {"tool": "terminal", "call": json.dumps({"command": f"printf status-{i}"}),
         "output": "ok", "timestamp": 21.0 + i}
        for i in range(8)
    ]
    # The display ledger contains only the last eight rows, but the hidden
    # floor ledger retains the send and must still block before the LLM.
    evidence = goals._EvidenceRows(later, voice_send_entries=[send] + later)
    monkeypatch.setattr(goals, "_call_goal_judge_llm", lambda *a: '{"verdict":"done","reason":"ok"}')

    verdict, reason, *_ = goals.judge_goal(
        "send", "done", evidence=evidence, voice_send_since=20.0,
    )
    assert verdict == "blocked"
    assert "deterministic voice-send floor" in reason


def test_goal_judge_ignores_gate_refusal_but_blocks_unapproved_send(monkeypatch):
    from hermes_cli import goals
    from tools import voice_send_gate

    called = []
    monkeypatch.setattr(goals, "_call_goal_judge_llm", lambda *a: called.append(1) or '{"verdict":"done","reason":"ok"}')
    from tools.registry import tool_error
    refused = [{"tool": "terminal", "call": json.dumps({"command": "slack message send hi"}),
                "output": tool_error(voice_send_gate._block("abc", "not approved"))}]
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
