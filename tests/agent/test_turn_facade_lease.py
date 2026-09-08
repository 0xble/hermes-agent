"""Unit tests for agent.turn_facade_lease (admission + lease bracket)."""
import threading
from types import SimpleNamespace

import pytest

from agent.turn_facade_lease import (
    LEASE_TTL_SECONDS,
    LEASE_WAIT_SECONDS,
    DurableTurnLease,
    admit_durable_turn_lease,
)


class _Db:
    def __init__(self, exists=True, acquired=True):
        self.exists = exists
        self.acquired = acquired
        self.events = []

    def get_session(self, session_id):
        return {"id": session_id} if self.exists else None

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        self.events.append(("acquire", session_id, holder))
        return self.acquired

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def release_session_turn_lease(self, session_id, holder):
        self.events.append(("release", session_id, holder))


def _agent(db, **overrides):
    agent = SimpleNamespace(
        _session_db=db,
        session_id="s1",
        _persist_disabled=False,
        _interrupt_requested=False,
        _interrupt_message=None,
        _execution_thread_id=None,
        _session_turn_lease_refresh_interval=60.0,
        statuses=[],
    )
    agent._emit_status = agent.statuses.append
    agent._emit_warning = agent.statuses.append
    agent._touch_activity = lambda *a, **k: None
    agent._liveness_activity_lock = lambda: threading.Lock()
    for k, v in overrides.items():
        setattr(agent, k, v)
    return agent


def _admit(agent, history=None):
    return admit_durable_turn_lease(
        agent,
        session_id="s1",
        relay_turn_id="s1:t:abcd",
        task_context={"session_id": "s1", "task_id": "t", "platform": "cli"},
        conversation_history=history,
    )


def test_no_lease_without_durable_row_or_when_persist_disabled():
    seed = [{"role": "user", "content": "hi"}]
    admission = _admit(_agent(_Db(exists=False)), seed)
    assert admission.lease is None and admission.early_result is None
    assert admission.conversation_history is seed

    db = _Db()
    admission = _admit(_agent(db, _persist_disabled=True), seed)
    assert admission.lease is None and db.events == []


def test_admission_sets_holder_attrs_and_release_clears_them(monkeypatch):
    monkeypatch.setattr(
        "agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0)
    )
    db = _Db()
    agent = _agent(db)
    admission = _admit(agent)
    lease = admission.lease
    assert isinstance(lease, DurableTurnLease)
    assert agent._session_db_created is True
    assert agent._active_session_turn_lease_holder == lease.holder
    assert agent._active_session_turn_lease_ttl_seconds == LEASE_TTL_SECONDS
    assert lease.holder.startswith("pid=") and ":platform=cli" in lease.holder
    assert lease.watchdog is None and lease.timer_handles == []
    assert lease.is_turn_active() is False

    lease.stop_refresher()
    lease.join_threads()
    lease.clear_interrupt()
    lease.release()
    assert db.events == [("acquire", "s1", lease.holder), ("release", "s1", lease.holder)]
    assert agent._active_session_turn_lease_holder is None
    assert agent._active_session_turn_lease_ttl_seconds is None


def test_timeout_and_interrupt_early_results():
    agent = _agent(_Db(acquired=False))
    admission = _admit(agent, [{"role": "user", "content": "x"}])
    assert admission.lease is None
    assert admission.early_result["failed"] is True
    assert admission.early_result["error"] == "session_turn_lease_timeout:s1"
    assert admission.early_result["messages"] == [{"role": "user", "content": "x"}]

    agent = _agent(_Db(acquired=False), _interrupt_requested=True, _interrupt_message="stop")
    agent.clear_interrupt = lambda: None
    admission = _admit(agent)
    assert admission.early_result["interrupted"] is True
    assert admission.early_result["interrupt_message"] == "stop"


def test_interrupt_turn_only_while_active():
    agent = _agent(_Db())
    calls = []
    agent.interrupt = lambda msg, **kw: calls.append(msg)
    lease = DurableTurnLease(agent, agent._session_db, "s1", "h")
    lease._interrupt_turn("lost")  # inactive: ignored
    assert calls == [] and lease.interrupt_message is None
    lease.turn_active = True
    lease._interrupt_turn("lost")
    assert calls == ["lost"] and lease.interrupt_message == "lost"
    lease.deactivate_after_liveness_abort()
    assert lease.stop.is_set() and lease.is_turn_active() is False


def test_delegated_resume_acquires_without_wait_and_reloads_compression_tip():
    class ResumeDb(_Db):
        def acquire_session_turn_lease(self, session_id, holder, **kwargs):
            self.acquire_kwargs = kwargs
            return super().acquire_session_turn_lease(session_id, holder, **kwargs)
        def resolve_resume_session_id(self, session_id):
            return "s2"
        def get_resume_conversations(self, session_id):
            assert session_id == "s2"
            return ([{"role": "assistant", "content": "prior"}], [])
    db = ResumeDb()
    agent = _agent(db, _delegation_resume_needs_reload=True,
                   _delegation_resume_fail_if_busy=True)
    task_context = {"session_id": "s1", "task_id": "t", "platform": "cli"}
    admission = admit_durable_turn_lease(
        agent, session_id="s1", relay_turn_id="s1:t:resume",
        task_context=task_context, conversation_history=None)
    assert db.acquire_kwargs["wait_seconds"] == 0.0
    assert agent.session_id == "s2" and task_context["session_id"] == "s2"
    assert admission.conversation_history == [{"role": "assistant", "content": "prior"}]
    assert admission.lease is not None
    admission.lease.release()


def test_resume_reload_failure_releases_lease_and_restores_grant():
    from tools.delegate_tool import _restore_unadmitted_resume_grant

    class ResumeDb(_Db):
        def resolve_resume_session_id(self, session_id):
            return session_id
        def get_resume_conversations(self, _session_id):
            raise RuntimeError("reload failed")
        def release_delegated_resumes(self, session_ids, *, claim_id):
            self.events.append(("restore", tuple(session_ids), claim_id))
            return True

    db = ResumeDb()
    agent = _agent(db, _delegation_resume_needs_reload=True,
                   _delegation_resume_claim_id="claim-a", _delegation_resume_admitted=False)
    with pytest.raises(RuntimeError, match="reload failed"):
        _admit(agent)
    assert agent._delegation_resume_admitted is False
    assert db.events[0][0] == "acquire" and db.events[1][0] == "release"
    assert _restore_unadmitted_resume_grant(agent) is True
    assert db.events[2] == ("restore", ("s1",), "claim-a")


def test_resume_thread_build_failure_releases_lease_and_restores_grant(monkeypatch):
    from tools.delegate_tool import _restore_unadmitted_resume_grant

    class ResumeDb(_Db):
        def release_delegated_resumes(self, session_ids, *, claim_id):
            self.events.append(("restore", tuple(session_ids), claim_id))
            return True

    monkeypatch.setattr(DurableTurnLease, "build_threads",
                        lambda _lease: (_ for _ in ()).throw(RuntimeError("thread build failed")))
    db = ResumeDb()
    agent = _agent(db, _delegation_resume_claim_id="claim-a", _delegation_resume_admitted=False)
    with pytest.raises(RuntimeError, match="thread build failed"):
        _admit(agent)
    assert agent._delegation_resume_admitted is False
    assert db.events[0][0] == "acquire" and db.events[1][0] == "release"
    assert _restore_unadmitted_resume_grant(agent) is True
    assert db.events[2] == ("restore", ("s1",), "claim-a")


def test_resume_admission_marks_grant_only_after_threads_build(monkeypatch):
    from tools.delegate_tool import _restore_unadmitted_resume_grant

    observed = []
    monkeypatch.setattr(DurableTurnLease, "build_threads",
                        lambda lease: observed.append(lease.agent._delegation_resume_admitted))
    db = _Db()
    agent = _agent(db, _delegation_resume_claim_id="claim-a", _delegation_resume_admitted=False)
    admission = _admit(agent)
    assert observed == [False]
    assert agent._delegation_resume_admitted is True
    assert _restore_unadmitted_resume_grant(agent) is False
    assert admission.lease is not None
    admission.lease.release()


def test_duplicate_delegated_resume_fails_closed_without_history_read():
    class BusyDb(_Db):
        def get_resume_conversations(self, _session_id):
            raise AssertionError("history must not be read before lease admission")
    db = BusyDb(acquired=False)
    agent = _agent(db, _delegation_resume_needs_reload=True,
                   _delegation_resume_fail_if_busy=True)
    admission = _admit(agent)
    assert admission.lease is None
    assert admission.early_result["error"] == "session_turn_lease_timeout:s1"


def test_ordinary_waited_lease_reload_keeps_row_ids_and_repairs_alternation(tmp_path):
    """The ordinary waited branch reloads the real model projection, not a lossy fallback."""
    from hermes_state import SessionDB

    class WaitedSessionDB(SessionDB):
        def acquire_session_turn_lease(self, *args, on_wait=None, **kwargs):
            self.wait_seconds = kwargs["wait_seconds"]
            assert on_wait is not None
            on_wait(1.0)
            return super().acquire_session_turn_lease(*args, on_wait=on_wait, **kwargs)

    db = WaitedSessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="tool")
    db.append_message("s1", role="user", content="prompt")
    db.append_message("s1", role="assistant", content="candidate", finish_reason="verification_required")
    db.append_message("s1", role="assistant", content="verified", finish_reason="stop")
    agent = _agent(db)

    admission = _admit(agent)
    history = admission.conversation_history
    assert db.wait_seconds == LEASE_WAIT_SECONDS
    assert history and all(isinstance(message.get("_row_id"), int) for message in history)
    assert all(left["role"] != right["role"] for left, right in zip(history, history[1:]))
    assert [message["content"] for message in history] == ["prompt", "verified"]
    assert admission.lease is not None
    admission.lease.release()
