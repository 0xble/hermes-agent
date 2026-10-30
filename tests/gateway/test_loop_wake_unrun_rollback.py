"""A loop wake the adapter accepted but whose turn never reached the post-turn hooks.

The scheduler claims the tick (``awaiting_response``) and stamps it on the wake. Once the adapter
accepts the wake, only that wake's post-turn loop completion settles the tick: an unstamped later
turn deliberately leaves a tick this process claimed alone. A turn that exits after admission but
before the hooks (``max_concurrent_sessions`` rejection, turn-lease timeout) must therefore skip
this started tick itself, or the loop stays in flight and the watcher never fires it again.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.session import SessionSource


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    from hermes_state_registry import close_all_under

    goals._DB_CACHE.clear()
    goals._get_session_db()  # warm from sync context; the loop-thread writes then find it cached
    yield home
    goals._DB_CACHE.clear()
    close_all_under(home)


_ROUTE = {"platform": "telegram", "chat_id": "-1001", "chat_type": "group", "user_id": "12345"}


def _fired_loop_wake(runner, sid):
    """Claim a due tick the way the scheduler does and build its stamped internal wake."""
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import loop_wake_metadata

    mgr = LoopManager(session_id=sid)
    mgr.set("check the queue", interval_seconds=300, route=_ROUTE)
    mgr.state.next_due_at = 0
    mgr._save()
    wakeup = mgr.fire_tick()
    assert wakeup and mgr.state.awaiting_response
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345")
    event = runner._synthetic_prompt_event(source, wakeup, internal=True, reply_expected=False)
    event.metadata.update(loop_wake_metadata(sid, mgr.state))
    event._gateway_accepted = True  # the adapter admitted it: the scheduler will not abandon it
    return event


def _runner(monkeypatch, tmp_path):
    from tests.gateway.test_duplicate_user_message import _bootstrap

    runner = _bootstrap(monkeypatch, tmp_path)
    runner._gateway_started_at = time.time() - 60  # this process claimed the tick
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", ["max_concurrent_sessions", "turn_lease_timeout"])
async def test_accepted_loop_wake_that_never_ran_rolls_back_its_tick(
    hermes_home, monkeypatch, tmp_path, exit_path,
):
    from gateway.turn_lease import TurnLeaseTimeoutError
    from hermes_cli.loops import LoopManager, load_loop

    runner = _runner(monkeypatch, tmp_path)
    entry = runner.session_store.get_or_create_session.return_value
    sid = entry.session_id
    event = _fired_loop_wake(runner, sid)
    if exit_path == "max_concurrent_sessions":
        runner._claim_active_session_slot = lambda _key, _source: (None, "too many active sessions")
    else:
        runner._hmwa_acquire_turn_lease = AsyncMock(side_effect=TurnLeaseTimeoutError(
            sid, owner_key=entry.session_key, generation=1, wait_seconds=0))
    runner._run_agent = AsyncMock(return_value={"final_response": "ran", "messages": []})

    await runner._handle_message(event)

    runner._run_agent.assert_not_awaited()
    stored = load_loop(sid)
    assert stored.awaiting_response is False
    assert stored.ticks_fired == 1  # started ticks are skipped, never replayed
    # The skipped tick is not replayed immediately; the normal cadence schedules the next one.
    assert LoopManager(session_id=sid).fire_tick() is None
    stored.next_due_at = 0
    from hermes_cli.loops import save_loop
    save_loop(sid, stored)
    assert LoopManager(session_id=sid).fire_tick()


@pytest.mark.asyncio
async def test_loop_wake_whose_turn_ran_settles_its_tick_exactly_once(hermes_home, monkeypatch, tmp_path):
    from hermes_cli.loops import LoopManager, load_loop

    runner = _runner(monkeypatch, tmp_path)
    sid = runner.session_store.get_or_create_session.return_value.session_id
    event = _fired_loop_wake(runner, sid)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "queue is empty", "messages": [], "history_offset": 0})

    completes, abandons = [], []
    real_complete, real_abandon = LoopManager.complete_tick, LoopManager.abandon_tick

    def complete(self, *args, **kwargs):
        completes.append(kwargs.get("tick"))
        return real_complete(self, *args, **kwargs)

    def abandon(self, *args, **kwargs):
        abandons.append(kwargs.get("tick"))
        return real_abandon(self, *args, **kwargs)

    with patch.object(LoopManager, "complete_tick", complete), \
            patch.object(LoopManager, "abandon_tick", abandon):
        await runner._handle_message(event)

    runner._run_agent.assert_awaited_once()
    stored = load_loop(sid)
    assert stored.awaiting_response is False
    assert stored.ticks_fired == 1
    assert stored.status == "active" and stored.next_due_at > time.time()
    assert len(completes) == 1 and not abandons


@pytest.mark.asyncio
async def test_unrun_rollback_only_undoes_the_tick_the_wake_carried(hermes_home, monkeypatch, tmp_path):
    """A stop and re-set between the claim and the early exit put a newer tick in flight; the
    stale wake's rollback must not release it."""
    from hermes_cli.loops import LoopManager, load_loop

    runner = _runner(monkeypatch, tmp_path)
    sid = runner.session_store.get_or_create_session.return_value.session_id
    event = _fired_loop_wake(runner, sid)
    time.sleep(0.01)
    newer = LoopManager(session_id=sid)
    newer.set("a different loop", interval_seconds=300, route=_ROUTE)
    newer.state.next_due_at = 0
    newer._save()
    assert newer.fire_tick()

    await runner._release_unsettled_loop_tick(event, ran=False)

    stored = load_loop(sid)
    assert stored.prompt == "a different loop"
    assert stored.awaiting_response is True and stored.ticks_fired == 1


@pytest.mark.asyncio
async def test_unrun_rollback_runs_off_the_event_loop(hermes_home, monkeypatch, tmp_path):
    import threading

    from hermes_cli.loops import LoopManager

    runner = _runner(monkeypatch, tmp_path)
    sid = runner.session_store.get_or_create_session.return_value.session_id
    event = _fired_loop_wake(runner, sid)
    loop_thread = threading.get_ident()
    threads = []
    real_abandon = LoopManager.abandon_tick

    def abandon(self, *args, **kwargs):
        threads.append(threading.get_ident())
        return real_abandon(self, *args, **kwargs)

    with patch.object(LoopManager, "abandon_tick", abandon):
        await runner._release_unsettled_loop_tick(event, ran=False)
    assert threads and loop_thread not in threads


def test_control_request_text_fallback_says_buttons_are_required():
    import asyncio

    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class TextOnly(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
            self.sent = []

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            pass

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {"id": chat_id}

    adapter = TextOnly()
    asyncio.run(adapter.send_control_request("c1", "Pause the loop in session X?", "req-123"))
    (text,) = adapter.sent
    assert "req-123" in text
    assert "button" in text.lower()
    assert "unavailable on this platform" not in text
