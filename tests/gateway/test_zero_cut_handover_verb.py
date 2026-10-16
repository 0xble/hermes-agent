"""Focused tests for the active-side zero-cut handover verb."""

import asyncio

import pytest
from types import SimpleNamespace

from gateway.config import GatewayConfig
from gateway.zero_cut_handover_drain import request_handover
from plugins.platforms.telegram.disconnect_handover import handover_stop_polling


class _Queue:
    def __init__(self, values):
        self.values = list(values)

    def empty(self):
        return not self.values


class _Updater:
    running = True

    async def stop(self):
        self.running = False
        while self.app.update_queue.values:
            value = self.app.update_queue.values.pop(0)
            self.app.processed.append(value)
            self.app.adapter._inflight_update_ids.pop(str(value), None)
            await asyncio.sleep(0)


class _Runner:
    def __init__(self, enabled=True, active=0):
        self.config = GatewayConfig(zero_cut_handover_enabled=enabled)
        self.adapters = {}
        self._running = True
        self._draining = False
        self._external_drain_active = False
        self._active = active
        self.exited = False
        self.tasks = set()

    def _running_agent_count(self):
        return self._active

    def _retain_background_task(self, task):
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def stop(self):
        self._running = False

    def _request_clean_exit(self, reason):
        self.exited = reason


def test_handover_stops_polling_and_settles_queued_updates_without_destroying_bot():
    async def scenario():
        adapter = SimpleNamespace(_inflight_update_ids={"1": 1, "2": 1})
        bot = object()
        updater = _Updater()
        app = SimpleNamespace(
            updater=updater, update_queue=_Queue([1, 2]), processed=[], bot=bot,
            adapter=adapter,
        )
        updater.app = app
        adapter._app = app
        result = await handover_stop_polling(adapter)
        assert result == {"settled": True}
        assert app.processed == [1, 2]
        assert app.bot is bot

    asyncio.run(scenario())


def test_handover_verb_accepts_once_and_reports_status(monkeypatch):
    async def scenario():
        runner = _Runner()
        monkeypatch.setattr("cron.scheduler_review.stop_reviews", lambda: None)
        monkeypatch.setattr("gateway.zero_cut_standby.is_standby", lambda: False)
        first = request_handover(runner, asyncio.get_running_loop())
        second = request_handover(runner, asyncio.get_running_loop())
        assert first["accepted"] is True
        assert second["accepted"] is True
        assert second["updates_settled"] is False
        assert runner._external_drain_active is True
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_handover_waits_for_buffered_text_batches_before_settling():
    async def scenario():
        adapter = SimpleNamespace(_inflight_update_ids={}, _pending_text_batches={"chat": object()})
        app = SimpleNamespace(updater=None, update_queue=_Queue([]), bot=object())
        adapter._app = app

        async def flush_later():
            await asyncio.sleep(0.05)
            adapter._pending_text_batches.clear()

        flusher = asyncio.create_task(flush_later())
        result = await handover_stop_polling(adapter)
        await flusher
        assert result == {"settled": True}
        assert adapter._pending_text_batches == {}

    asyncio.run(scenario())


def test_idle_handover_stops_outside_background_tasks_and_exits_cleanly(monkeypatch):
    from gateway import zero_cut_handover_drain as drain

    async def scenario():
        runner = _Runner(active=1)
        runner._background_tasks = set()
        stopped = []

        async def stop():
            # _stop_impl cancels everything in _background_tasks; the stop task must survive that.
            for task in list(runner._background_tasks):
                task.cancel()
            stopped.append(asyncio.current_task() not in runner._background_tasks)

        runner.stop = stop
        runner._retain_background_task = lambda task: (runner._background_tasks.add(task), task)[1]
        runner._zero_cut_handover_state = {
            "accepted": True, "updates_settled": True, "token_lock_released": True,
            "active_turns": 1, "idle": False, "failed": None}
        waiter = runner._retain_background_task(asyncio.create_task(drain._wait_for_idle(runner)))
        await asyncio.sleep(0.12)
        assert stopped == []  # an active turn keeps A alive
        runner._active = 0
        await asyncio.wait_for(waiter, 1)
        await asyncio.wait_for(runner._zero_cut_stop_task, 1)
        assert stopped == [True]
        assert drain.status(runner)["idle"] is True

    asyncio.run(scenario())


def test_handover_verb_refuses_disabled_and_standby(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        disabled = _Runner(enabled=False)
        assert request_handover(disabled, loop) == {"accepted": False, "refused": "disabled"}
        standby = _Runner()
        monkeypatch.setattr("gateway.zero_cut_standby.is_standby", lambda: True)
        assert request_handover(standby, loop) == {"accepted": False, "refused": "standby"}
    finally:
        loop.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state, notified", [
    ({"token_lock_released": True, "failed": None}, False),   # completed handover: nothing is cut
    ({"token_lock_released": False, "failed": None}, True),   # ordinary stop keeps today's notice
    ({"token_lock_released": True, "failed": "boom"}, True),
    (None, True),
])
async def test_completed_handover_stop_sends_no_shutting_down_notice(monkeypatch, state, notified):
    """Found in a real drill: the old slot exited after its last turn and still told the chat
    'Hermes is shutting down — your current task will be interrupted', a false alarm."""
    from gateway.run_shutdown import GatewayShutdownMixin

    runner = object.__new__(GatewayShutdownMixin)
    runner._restart_requested = False
    runner._running = True
    runner._draining = False
    runner._zero_cut_handover_state = state
    runner._running_agents = {}
    runner._clear_plugin_message_injector = lambda: None
    runner._mark_api_runs_shutdown_requested = lambda: None
    runner._cancel_secondary_profile_reconnect_tasks = lambda: asyncio.sleep(0)
    runner._stop_systemd_watchdog = lambda: asyncio.sleep(0)
    runner._stop_hosted_room_worker = lambda timeout: asyncio.sleep(0, result=True)
    calls = []

    async def notify(*args, **kwargs):
        calls.append("notice")

    runner._notify_active_sessions_of_shutdown = notify
    monkeypatch.setattr("cron.scheduler_review.stop_reviews", lambda: None)
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0, active_agents={}, timed_out=False,
                                           started_at=0.0)
    await runner._stop_begin_teardown(ctx)
    assert (calls == ["notice"]) is notified
