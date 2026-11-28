"""A background batch still waiting for an async pool slot when the gateway goes down never reached ``record_start``.
Its children must still leave AUTO_ON_BOOT records (``record_queued``), the unit must stay silent instead of waking
the parent with "never started", and the scheduler must start each child on its original goal.

Only the model is scripted; dispatch, the async pool and its queue, the durable records, boot recovery and the
scheduler's own tick are real, against a temp HERMES_HOME.
"""

import json
import sqlite3
import threading
from unittest.mock import patch

from tests.tools.test_delegate_auto_resume import _enable, _repo, _scheduler, _settled
from tests.tools.test_delegate_continue import _parent, _ScriptedModel
from tests.tools.test_delegate_resumable import _db_rows, _forget_process_state

_DEAD = 2 ** 22 + 7
_GOALS = ("queued alpha", "queued beta")


class _PoolHolder(_ScriptedModel):
    """A child whose goal says ``hold the pool`` blocks in its first turn until released, then reads as stopped."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.holding = threading.Semaphore(0)

    def __call__(self, **kw):
        child = super().__call__(**kw)
        ok = child.run_conversation.side_effect

        def run_conversation(user_message, conversation_history=None, **rest):
            result = ok(user_message, conversation_history=conversation_history, **rest)
            if "hold the pool" in user_message:
                self.holding.release()
                self.release.wait(10)
                return {**result, "completed": False, "interrupted": True, "final_response": "Operation interrupted."}
            return result
        child.run_conversation.side_effect = run_conversation
        return child


def _queued_batch(tmp_path, model):
    """Two pool-holding calls fill ``max_concurrent_children: 2``; a 2-task batch then waits for a slot."""
    from tools.delegate_tool import delegate_task
    from tools.terminal_tool import record_session_cwd
    _enable(max_concurrent_children=2)
    parent = _parent()
    parent._current_task_id = "parent-task"
    record_session_cwd("parent-task", str(_repo(tmp_path, "repo")))
    holders = []
    for n in range(2):
        out = json.loads(delegate_task(tasks=[{"goal": f"hold the pool {n}"}], background=True, parent_agent=parent))
        assert out.get("status") == "dispatched", out
        assert model.holding.acquire(timeout=10)
        holders.append(out["delegation_id"])
    out = json.loads(delegate_task(tasks=[{"goal": g} for g in _GOALS], background=True, parent_agent=parent))
    assert out.get("status") == "queued", out
    return parent, out["delegation_id"], holders


def _queued_rows(parent):
    return {record["goal"]: (sid, state, record) for sid, (state, _pinned, record) in _db_rows(parent).items()
            if record.get("goal") in _GOALS}


def _ledger(parent, delegation_id):
    conn = sqlite3.connect(parent._session_db.db_path)  # health: allow HX015 -- test reads a temp-home store
    try:
        return conn.execute("SELECT state, delivery_state FROM async_delegations WHERE delegation_id=?",
                            (delegation_id,)).fetchone()
    finally:
        conn.close()


def _as_new_process(parent):
    """What the next process sees: every run record and ledger row owned by a process that is gone."""
    conn = sqlite3.connect(parent._session_db.db_path)  # health: allow HX015 -- test seeds a temp-home store
    with conn:
        conn.execute("UPDATE resumable_subagents SET owner_pid=?, record_json=json_set(record_json, '$.owner_pid', ?)",
                     (_DEAD, _DEAD))
    conn.close()
    _forget_process_state()
    _enable(max_concurrent_children=4)  # room for every resume at once: the pool is not under test here


def test_a_batch_queued_for_a_slot_at_a_restart_resumes_each_child_on_its_original_goal(tmp_path):
    from tools.async_delegation import interrupt_all
    from tools.delegate_tool_auto_resume import tick
    from tools.delegation_resume import GATEWAY_RESTART_CUT_REASON

    model = _PoolHolder()
    with patch("run_agent.AIAgent", side_effect=model):
        parent, unit, _holders = _queued_batch(tmp_path, model)
        interrupt_all(reason=GATEWAY_RESTART_CUT_REASON, resumable=True)
        model.release.set()
        _settled(unit)

        rows = _queued_rows(parent)
        assert sorted(rows) == sorted(_GOALS), rows  # each queued child left a record
        for _goal, (_sid, state, record) in rows.items():
            assert (state, record["resume_mode"], record["auto_promised"]) == ("resumable", "AUTO_ON_BOOT", True)
        assert _ledger(parent, unit) == ("interrupted", "delivered")  # silent: no "never started" wake, no cold retry

        _as_new_process(parent)
        dispatched = []
        for _ in range(8):  # one resume per endpoint per tick: the 2 pool holders and the 2 queued children
            before = len(dispatched)
            tick(_scheduler(parent, dispatched), capacity=4)
            for delegation_id in dispatched[before:]:
                _settled(delegation_id)
            if len(dispatched) >= 4:
                break

    rows = _queued_rows(parent)
    for goal, (sid, state, record) in rows.items():
        turn = next(t for t in model.turns if t["session_id"] == record["session_id"])
        assert turn["message"].startswith(goal) and not turn["history"], turn  # its goal, afresh
        assert state == "completed", (goal, state)


def test_a_batch_queued_for_a_slot_when_its_owner_died_is_recovered_silently_for_the_scheduler(tmp_path):
    from tools.async_delegation import interrupt_all, recover_abandoned_delegations

    model = _PoolHolder()
    with patch("run_agent.AIAgent", side_effect=model):
        parent, unit, holders = _queued_batch(tmp_path, model)
        conn = sqlite3.connect(parent._session_db.db_path)  # health: allow HX015 -- test seeds a temp-home store
        with conn:  # the owner died (SIGKILL) while the batch still waited: nothing settled its rows
            conn.execute("UPDATE async_delegations SET owner_pid=? WHERE delegation_id=?", (_DEAD, unit))
            conn.execute("UPDATE resumable_subagents SET owner_pid=? WHERE state='running' AND "
                         "json_extract(record_json, '$.unit_id')=?", (_DEAD, unit))
        conn.close()
        try:
            recover_abandoned_delegations()
            rows = _queued_rows(parent)
            assert sorted(rows) == sorted(_GOALS), rows
            for _goal, (_sid, state, record) in rows.items():
                assert (state, record["resume_mode"], record["auto_promised"]) == ("resumable", "AUTO_ON_BOOT", True)
            assert _ledger(parent, unit) == ("interrupted", "delivered")
        finally:
            interrupt_all(reason="test teardown")
            model.release.set()
            for delegation_id in holders:
                _settled(delegation_id)
