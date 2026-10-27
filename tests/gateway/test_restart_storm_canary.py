"""Restart-storm canary: a typed message stays fast while a boot's worth of background work drains.

Reproduces the shape of the 2026-10-25 Personal gateway restart (about 50 interrupted sessions to
auto-resume, 25 goal wakes and 13 delegation retries due at once) through the production seams:
the real Telegram adapter, ``GatewayRunner._handle_message``, the replay scheduler and
``admit_internal_event``. Only the model call (``_handle_message_with_agent``) is a stand-in, so
the assertions are about admission: the typed message's model call must start within seconds,
no more than ``gateway.background_turn_concurrency`` background turns may run at once, and every
resume and wake must still run.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from gateway import run_background_admission
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionEntry
from gateway.wake import WakeNotAccepted, admit_internal_event
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tests.waiting import async_wait_until

CAP = 4
RESUMES, GOAL_WAKES, RETRIES = 50, 25, 13
TURN_S = 0.05


def _telegram_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="1"))
    adapter.send_typing = AsyncMock()
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True, message_id="1"))
    adapter._keep_typing = AsyncMock()
    adapter._stop_typing_refresh = AsyncMock()
    adapter._run_processing_hook = AsyncMock()
    return adapter


class _Model:
    """Stands in for the model call and records when each turn's call started."""

    def __init__(self):
        self.started: dict[str, float] = {}
        self.background = 0
        self.peak_background = 0

    async def run_turn(self, event, _source, quick_key, _generation):
        self.started.setdefault(quick_key, time.monotonic())
        typed = not event.internal
        if not typed:
            self.background += 1
            self.peak_background = max(self.peak_background, self.background)
        try:
            await asyncio.sleep(TURN_S)  # health: allow HX013 -- the stand-in model call's duration
        finally:
            if not typed:
                self.background -= 1
        event._agent_turn_succeeded = True
        return "ok"


def _wire(monkeypatch):
    monkeypatch.setattr(run_background_admission, "BACKGROUND_TURN_SPACING_S", 0.0)
    runner, adapter = make_restart_runner(_telegram_adapter())
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner.config.background_turn_concurrency = CAP
    runner._handle_message = GatewayRunner._handle_message.__get__(runner, GatewayRunner)
    runner._release_running_agent_state = GatewayRunner._release_running_agent_state.__get__(runner, GatewayRunner)
    runner._check_slash_access = lambda *a, **k: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._is_session_run_current = lambda _key, _generation: True
    runner._invalidate_session_run_generation = lambda *a, **k: 0
    runner._claim_active_session_slot = lambda _key, _source: (object(), None)
    runner._active_session_leases = {}
    runner._busy_ack_ts = {}
    runner._post_turn_goal_continuation = AsyncMock()
    runner._send_interrupted_turn_notes = AsyncMock(return_value=0)
    runner.session_store.get_or_create_session.return_value = None
    model = _Model()
    runner._handle_message_with_agent = model.run_turn
    adapter.set_message_handler(runner._handle_message)
    adapter.gateway_runner = runner
    tasks: list[asyncio.Task] = []
    runner._retain_background_task = lambda task: tasks.append(task) or task
    sources = {}

    def resume_entry(i: int) -> SessionEntry:
        source = make_restart_source(chat_id=f"resume-{i}")
        key = runner._session_key_for_source(source)
        sources[key] = source
        return SessionEntry(
            session_key=key, session_id=f"sid-{i}", created_at=datetime.now(), updated_at=datetime.now(),
            origin=source, platform=Platform.TELEGRAM, chat_type="dm", resume_pending=True,
            resume_reason="restart_interrupted", last_resume_marked_at=datetime.now(),
        )

    entries = [resume_entry(i) for i in range(RESUMES)]
    runner.session_store._entries = {entry.session_key: entry for entry in entries}
    monkeypatch.setattr(runner, "_auto_resume_ready",
                        lambda entry: (adapter, sources[entry.session_key]), raising=False)
    return runner, adapter, model, entries, tasks


def _wake(runner, chat_id: str) -> MessageEvent:
    source = make_restart_source(chat_id=chat_id)
    return MessageEvent(
        text=f"[IMPORTANT: background result for {chat_id}]", message_type=MessageType.TEXT,
        source=source, internal=True,
        metadata={"notification_origin": "process_registry_synthetic",
                  "gateway_session_key": runner._session_key_for_source(source)},
    )


async def _deliver_until_admitted(adapter, event) -> None:
    """A wake source's retry loop: refusal keeps its claim and it tries again later."""
    while True:
        try:
            return await admit_internal_event(adapter, event)
        except WakeNotAccepted:
            await asyncio.sleep(0.02)  # health: allow HX013 -- stands in for the caller's retry cadence


@pytest.mark.asyncio
async def test_typed_message_starts_its_model_call_within_seconds_of_a_restart_storm(monkeypatch):
    runner, adapter, model, entries, tasks = _wire(monkeypatch)
    assert runner._schedule_resume_pending_sessions(candidates=entries, start_scheduler=True) == RESUMES
    wakes = [
        asyncio.create_task(_deliver_until_admitted(adapter, _wake(runner, f"{kind}-{i}")))
        for kind, count in (("goal", GOAL_WAKES), ("retry", RETRIES)) for i in range(count)
    ]
    await asyncio.sleep(0)

    typed_source = make_restart_source(chat_id="brian")
    typed_key = runner._session_key_for_source(typed_source)
    sent = time.monotonic()
    await adapter.handle_message(MessageEvent(text="Hello?", message_type=MessageType.TEXT, source=typed_source))
    await async_wait_until(lambda: typed_key in model.started, timeout=10, what="typed model call")
    assert model.started[typed_key] - sent < 3.0

    await asyncio.wait_for(asyncio.gather(*wakes), 60)
    background = {entry.session_key for entry in entries} | {
        runner._session_key_for_source(make_restart_source(chat_id=f"{kind}-{i}"))
        for kind, count in (("goal", GOAL_WAKES), ("retry", RETRIES)) for i in range(count)
    }
    await async_wait_until(lambda: background <= set(model.started), timeout=60,
                           what="every resume and wake ran")
    assert model.peak_background <= CAP
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 30)
    await runner._get_replay_scheduler().close()
