"""Only dispatcher-bound executed outcomes may support a Kanban handoff."""
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker(tmp_path, monkeypatch):
    # Install the network fence before importing judge/provider consumers.
    attempts = []

    def no_network(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, goals
    from tools import kanban_tools as kt
    from agent import auxiliary_client
    from hermes_state import SessionDB

    seen = []

    def judge(*args, **kwargs):
        seen.append(kwargs.get("tool_evidence", []))
        return "done", "fake judge", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    monkeypatch.setattr(kt, "judge_goal", judge)
    monkeypatch.setattr(auxiliary_client, "get_text_auxiliary_client", lambda *a, **kw: (object(), "fake"))
    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="verify artifact", assignee="default", goal_mode=True,
                             session_id="origin-not-worker")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        task = kb.claim_task(conn, tid)
    assert task is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    db = SessionDB(tmp_path / "state.db")
    agent = SimpleNamespace(session_id="worker-session", _session_db=db)
    yield SimpleNamespace(kb=kb, kbc=kbc, goals=goals, kt=kt, task=task, db=db,
                          agent=agent, seen=seen, home=tmp_path)
    db.close()
    assert attempts == []


def outcome(db, sid, cid, code=0):
    db.append_messages_batch(sid, [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "terminal", "arguments": '{"command":"check"}'}}]},
        {"role": "tool", "tool_name": "terminal", "tool_call_id": cid,
         "content": json.dumps({"exit_code": code, "output": "executed check", "path": "/tmp/artifact"})},
    ])


@pytest.mark.parametrize("surface", ["cli", "tool", "loop"])
@pytest.mark.parametrize("compressed", [False, True])
def test_launch_to_judge_uses_only_owned_executed_outcomes(worker, monkeypatch, surface, compressed):
    import cli
    from hermes_cli.kanban import _goal_mode_handoff_rejection
    w = worker
    w.db.create_session("origin-not-worker", source="cli")
    outcome(w.db, "origin-not-worker", "foreign")

    def run(**kwargs):
        w.db.create_session(w.agent.session_id, source="kanban")
        outcome(w.db, w.agent.session_id, "owned-before", 1)
        if compressed:
            old = w.agent.session_id
            w.db.end_session(old, "compression")
            w.db.create_session("delegate", source="tool", parent_session_id=old,
                                model_config={"_delegate_from": old})
            outcome(w.db, "delegate", "foreign-child")
            w.db.create_session("compressed", source="kanban", parent_session_id=old)
            w.agent.session_id = "compressed"
            outcome(w.db, w.agent.session_id, "owned-before", 1)  # retained compression copy
        outcome(w.db, w.agent.session_id, "owned-after")
        return {"final_response": "prose is not evidence"}

    w.agent.run_conversation = run
    app = SimpleNamespace(agent=w.agent, session_id=w.agent.session_id, conversation_history=[])
    with pytest.raises(SystemExit) as exit_info:
        cli._run_quiet_single_query(app, "work")
    assert exit_info.value.code == 0
    # Durable/manual handoff works without the worker environment or session identity.
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setenv("HERMES_SESSION_ID", "origin-not-worker")
    with w.kbc.connect_closing() as conn:
        conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                     (json.dumps({"worker_session_id": "origin-not-worker", "tool_evidence": [{"positive": True}]}),
                      w.task.current_run_id))
        conn.commit()
    if surface == "cli":
        _goal_mode_handoff_rejection(w.task, "claimed success")
    elif surface == "tool":
        w.kt._goal_gate("kanban_complete", w.task, w.task.id, "claimed success")
    else:
        w.goals.run_kanban_goal_loop(task_id=w.task.id, goal_text=w.task.title,
            run_turn=lambda p: "still open", task_status_fn=lambda: "running", block_fn=lambda p: None,
            max_turns=1, first_response="claimed success")
    assert len(w.seen[-1]) == 2
    assert {item["tool_call_id"] for item in w.seen[-1]} == {"owned-before", "owned-after"}
    assert [item["negative"] for item in w.seen[-1]] == [True, False]


@pytest.mark.parametrize("boundary", ["manual", "claim", "child", "profile", "rebind", "retry", "superseded", "migration",
                                      "historical", "reset", "branch", "compacted", "bounded"])
def test_binding_fences_and_absence_are_not_claimed_evidence(worker, monkeypatch, boundary):
    from hermes_cli.kanban_evidence import bind_worker_session, collect_kanban_evidence
    from agent.delegation_context import delegated_child_context
    w = worker
    if boundary == "historical":
        w.db.create_session(w.agent.session_id, source="kanban")
        outcome(w.db, w.agent.session_id, "pre-attempt")
    if boundary == "manual":
        monkeypatch.delenv("HERMES_KANBAN_TASK")
    elif boundary == "claim":
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "stale")
    elif boundary == "profile":
        monkeypatch.setenv("HERMES_HOME", str(w.home / "other"))
    elif boundary == "migration":
        with w.kbc.connect_closing() as conn:
            for col in ("worker_session_id", "worker_home", "worker_start_message_id"):
                conn.execute(f"ALTER TABLE task_runs DROP COLUMN {col}")
            conn.commit()
        w.kbc._INITIALIZED_PATHS.clear()
        w.kb.init_db()
        assert collect_kanban_evidence(w.task.id) == []
    if boundary == "child":
        with delegated_child_context("unrelated-child"):
            assert not bind_worker_session(w.agent)
    else:
        bound = bind_worker_session(w.agent)
        assert bound == (boundary not in {"manual", "claim", "profile", "historical"})
    w.db.create_session(w.agent.session_id, source="kanban")
    outcome(w.db, w.agent.session_id, "owned")
    if boundary == "retry":
        assert bind_worker_session(w.agent)  # must not advance the start watermark
    if boundary == "rebind":
        assert not bind_worker_session(SimpleNamespace(session_id="different", _session_db=w.db))
    if boundary == "superseded":
        with w.kbc.connect_closing() as conn:
            conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (w.task.id,))
            conn.commit()
    if boundary in {"reset", "branch"}:
        w.db.end_session(w.agent.session_id, "compression")
        marker = "_reset_from" if boundary == "reset" else "_branched_from"
        w.db.create_session("not-owned", source="cli", parent_session_id=w.agent.session_id,
                            model_config={marker: w.agent.session_id})
        outcome(w.db, "not-owned", "foreign-successor")
    if boundary == "compacted":
        w.db._write_sql(
            "UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ?", (w.agent.session_id,))
    if boundary == "bounded":
        for i in range(280):
            outcome(w.db, w.agent.session_id, f"bounded-{i}")
    evidence = collect_kanban_evidence(w.task.id, expected_run_id=w.task.current_run_id)
    if boundary == "bounded":
        assert len(evidence) == 32
        assert evidence[-1]["tool_call_id"] == "bounded-279"
    elif boundary in {"rebind", "retry", "migration", "reset", "branch", "compacted"}:
        assert [item["tool_call_id"] for item in evidence] == ["owned"]
    else:
        assert evidence == []
