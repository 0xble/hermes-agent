"""The gateway's idle wakeup ticker resumes a parked /goal whose barrier lifted with no turn.

Reproduces the live wedge: a goal parked on a background process that a gateway restart killed.
The completion notice died with the old process, so before this fix the goal stayed parked until an
unrelated user message arrived (observed: 14.5 h). The scan must inject exactly one continuation
through the adapter, clear the barrier only after admission, and defer while the chat is busy or a
restart auto-resume owns it.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_cli import goals

PROC = "proc_d2196a6c1051"
SID = "20260923_095014_6dc7f942"
KEY = "agent:main:telegram:dm:42:213161"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    reset_hermes_home_override(token)
    goals._DB_CACHE.clear()


class _Adapter:
    def __init__(self):
        self.handled = []
        self.sent = []
        self._message_handler = object()
        self._active_sessions = {}
        self._pending_messages = {}

    accept = True

    async def handle_message(self, event):
        # Mirrors BasePlatformAdapter: a refusal returns normally with _gateway_accepted=False.
        event._gateway_accepted = self.accept
        if self.accept:
            self.handled.append(event)

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)
        return SimpleNamespace(success=True)


def _runner(adapter, entry):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")})
    runner._running = True
    runner._running_agents = {}
    def _get_marker(key):
        if key != KEY or not entry.resume_pending:
            return None
        return (entry.session_id, getattr(entry, "resume_marker_token", None),
                getattr(entry, "last_resume_marked_at", None))

    def _clear_marker(key, *, expected_marker=None):
        if key != KEY or not entry.resume_pending:
            return False
        current = _get_marker(key)
        if expected_marker is not None and expected_marker != current:
            return False
        entry.resume_pending = False
        return True

    runner.session_store = SimpleNamespace(
        lookup_by_session_id=lambda sid: entry if sid == SID else None,
        get_resume_pending_marker=_get_marker,
        clear_resume_pending=_clear_marker,
    )
    runner._restored_source = lambda e: e.origin
    runner._delivery_adapter_for = lambda source: adapter
    runner._is_session_running = lambda key: False
    runner._queue_depth = lambda key, adapter=None: 0
    runner._thread_metadata_for_source = lambda source: None

    async def _in_executor(fn, *args):
        return fn(*args)

    runner._run_in_executor_with_context = _in_executor
    return runner


def _real_runner(adapter, store, entry):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")})
    runner._running = True
    runner._running_agents = {}
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._restored_source = lambda e: e.origin
    runner._delivery_adapter_for = lambda source: adapter
    runner._is_session_running = lambda key: False
    runner._queue_depth = lambda key, adapter=None: 0
    runner._thread_metadata_for_source = lambda source: None

    async def _in_executor(fn, *args):
        return fn(*args)

    runner._run_in_executor_with_context = _in_executor
    return runner


def _entry(**overrides):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", thread_id="213161")
    base = dict(session_id=SID, session_key=KEY, origin=source, suspended=False, resume_pending=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def _park_killed(
    home: Path, sid: str = SID, proc: str = PROC, *, exit_code: int = -15,
    completion_reason: str = "killed", termination_source: str = "kill_all",
) -> None:
    receipts = home / "logs" / "process-results"
    receipts.mkdir(parents=True)
    (receipts / f"{proc}.json").write_text(json.dumps({
        "id": proc, "exit_code": exit_code, "completion_reason": completion_reason,
        "termination_source": termination_source,
    }))
    mgr = goals.GoalManager(sid)
    mgr.set("Finish the repository CI rollout")
    mgr.wait_on_session(proc, reason="nightly verification still running")


def _real_store(home: Path):
    sessions_dir = home / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    store = SessionStore(
        sessions_dir=sessions_dir,
        config=GatewayConfig(sessions_dir=sessions_dir, write_sessions_json=False),
    )
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", thread_id="213161")
    entry = store.get_or_create_session(source)
    return store, entry


def _set_resume_pending(entry, *, marked_at, updated_at=None):
    entry.resume_pending = True
    entry.resume_reason = "restart_interrupted"
    entry.resume_marker_token = "stale-marker"
    entry.last_resume_marked_at = marked_at
    entry.updated_at = updated_at or marked_at


async def _one_scan(runner, monkeypatch):
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def _sleep(delay):
        calls["n"] += 1
        if calls["n"] >= 2:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    await GatewayRunner._loop_wakeup_watcher(runner, interval=0)


@pytest.mark.asyncio
async def test_old_generation_does_not_wake_parked_goal_after_transfer(hermes_home, monkeypatch):
    _park_killed(hermes_home)
    adapter = _Adapter()
    runner = _runner(adapter, _entry())
    runner._overlap_draining = True
    await _one_scan(runner, monkeypatch)
    assert adapter.handled == []
    assert goals.load_goal(SID).waiting_on_session == PROC

@pytest.mark.asyncio
async def test_watcher_resumes_goal_parked_on_restart_killed_process(hermes_home, monkeypatch):
    _park_killed(hermes_home)
    adapter = _Adapter()
    runner = _runner(adapter, _entry())

    await _one_scan(runner, monkeypatch)

    assert len(adapter.handled) == 1
    event = adapter.handled[0]
    assert event.text.startswith("[Continuing toward your standing goal]")
    assert f"{PROC} was killed by a gateway restart" in event.text
    assert event.metadata["gateway_session_key"] == KEY
    assert event.source.message_id is None
    assert adapter.sent == ["▶ Goal wait ended — resuming."]
    state = goals.load_goal(SID)
    assert state.status == "active" and state.waiting_on_session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["running", "active_guard", "queued", "resume_pending", "suspended"])
async def test_watcher_defers_when_the_chat_is_busy_or_owned(hermes_home, monkeypatch, busy):
    _park_killed(hermes_home)
    adapter = _Adapter()
    entry_kwargs = {"resume_pending": busy == "resume_pending", "suspended": busy == "suspended"}
    if busy == "resume_pending":
        entry_kwargs.update(
            resume_reason="restart_interrupted",
            resume_marker_token="fresh-marker",
            last_resume_marked_at=datetime.now(),
        )
    entry = _entry(**entry_kwargs)
    runner = _runner(adapter, entry)
    if busy == "running":
        runner._is_session_running = lambda key: True
    elif busy == "active_guard":
        adapter._active_sessions[KEY] = object()
    elif busy == "queued":
        runner._queue_depth = lambda key, adapter=None: 1

    await _one_scan(runner, monkeypatch)

    assert adapter.handled == []
    assert goals.load_goal(SID).waiting_on_session == PROC  # kept for the next scan


@pytest.mark.asyncio
async def test_stale_resume_pending_does_not_block_lifted_barrier(hermes_home, monkeypatch):
    _park_killed(hermes_home)
    adapter = _Adapter()
    entry = _entry(
        resume_pending=True,
        resume_reason="restart_interrupted",
        resume_marker_token="stale-marker",
        last_resume_marked_at=datetime.now() - timedelta(hours=2),
    )
    runner = _runner(adapter, entry)

    await _one_scan(runner, monkeypatch)

    assert len(adapter.handled) == 1
    assert "was killed by a gateway restart" in adapter.handled[0].text
    assert entry.resume_pending is False
    assert adapter.sent == ["▶ Goal wait ended — resuming."]
    assert goals.load_goal(SID).waiting_on_session is None


@pytest.mark.asyncio
async def test_silent_adapter_refusal_keeps_the_barrier(hermes_home, monkeypatch):
    """handle_message returning normally is not admission (routing refusal, key mismatch)."""
    _park_killed(hermes_home)
    adapter = _Adapter()
    adapter.accept = False
    runner = _runner(adapter, _entry())

    await _one_scan(runner, monkeypatch)

    assert adapter.sent == []  # no "resuming" notice for a turn that never started
    assert goals.load_goal(SID).waiting_on_session == PROC


@pytest.mark.asyncio
async def test_failed_injection_keeps_the_barrier_for_retry(hermes_home, monkeypatch):
    _park_killed(hermes_home)
    adapter = _Adapter()

    async def _boom(event):
        raise RuntimeError("adapter disconnected")

    adapter.handle_message = _boom
    runner = _runner(adapter, _entry())

    await _one_scan(runner, monkeypatch)

    assert goals.load_goal(SID).waiting_on_session == PROC


@pytest.mark.asyncio
async def test_real_store_cas_loss_keeps_marker_and_barrier(hermes_home, monkeypatch):
    store, entry = _real_store(hermes_home)
    old = datetime.now() - timedelta(hours=2)
    _set_resume_pending(entry, marked_at=old)
    store._save()
    proc = "proc_cas_lost0000"
    _park_killed(hermes_home, sid=entry.session_id, proc=proc)
    adapter = _Adapter()

    real_get_marker = store.get_resume_pending_marker

    def _refresh_after_snapshot(key):
        marker = real_get_marker(key)
        assert store.mark_resume_pending(key, reason="successor_restart")
        return marker

    monkeypatch.setattr(store, "get_resume_pending_marker", _refresh_after_snapshot)
    runner = _real_runner(adapter, store, entry)

    await _one_scan(runner, monkeypatch)

    assert adapter.handled == []
    assert store.lookup_by_session_id(entry.session_id).resume_pending is True
    assert goals.load_goal(entry.session_id).waiting_on_session == proc


@pytest.mark.asyncio
async def test_resume_marker_appearing_during_barrier_check_defers_without_clearing(
    hermes_home,
):
    _park_killed(hermes_home)
    adapter = _Adapter()
    entry = _entry()
    calls = 0

    async def _in_executor(func, *args):
        nonlocal calls
        result = func(*args)
        if calls == 0:
            entry.resume_pending = True
            entry.resume_reason = "successor_restart"
            entry.resume_marker_token = "successor-marker"
            entry.last_resume_marked_at = datetime.now()
        calls += 1
        return result

    runner = _runner(adapter, entry)
    clear_calls = []
    clear_marker = runner.session_store.clear_resume_pending

    def _clear_marker(key, **kwargs):
        clear_calls.append((key, kwargs))
        return clear_marker(key, **kwargs)

    runner.session_store.clear_resume_pending = _clear_marker
    runner._run_in_executor_with_context = _in_executor

    await GatewayRunner._goal_wakeup_fire_one(runner, SID)
    assert clear_calls == []
    assert adapter.handled == []
    assert entry.resume_pending is True
    assert goals.load_goal(SID).waiting_on_session == PROC


@pytest.mark.asyncio
async def test_real_store_legacy_marker_falls_back_to_old_updated_at(hermes_home, monkeypatch):
    store, entry = _real_store(hermes_home)
    old = datetime.now() - timedelta(hours=2)
    _set_resume_pending(entry, marked_at=None, updated_at=old)
    store._save()
    proc = "proc_legacy_marker0"
    _park_killed(hermes_home, sid=entry.session_id, proc=proc)
    adapter = _Adapter()
    runner = _real_runner(adapter, store, entry)

    await _one_scan(runner, monkeypatch)

    assert len(adapter.handled) == 1
    assert "was killed by a gateway restart" in adapter.handled[0].text
    assert store.lookup_by_session_id(entry.session_id).resume_pending is False
    assert goals.load_goal(entry.session_id).waiting_on_session is None


@pytest.mark.asyncio
async def test_real_store_continuation_reports_exit_code_one(hermes_home, monkeypatch):
    store, entry = _real_store(hermes_home)
    old = datetime.now() - timedelta(hours=2)
    _set_resume_pending(entry, marked_at=old)
    store._save()
    proc = "proc_exit_code_1"
    _park_killed(
        hermes_home, sid=entry.session_id, proc=proc, exit_code=1,
        completion_reason="exited", termination_source="",
    )
    adapter = _Adapter()
    runner = _real_runner(adapter, store, entry)

    await _one_scan(runner, monkeypatch)

    assert len(adapter.handled) == 1
    assert f"{proc} finished (exited, exit 1)" in adapter.handled[0].text
    assert goals.load_goal(entry.session_id).waiting_on_session is None
