"""Focused tests for the active-side zero-cut handover verb."""

import asyncio
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
