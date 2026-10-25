"""The restart-resume outcome must follow the turn the real adapter path actually runs.

Startup pre-claims each interrupted session with the pending sentinel. The resume event's own
``_handle_message`` therefore takes the busy path, parks itself in the adapter's pending slot and
returns; the adapter's post-turn drain then runs the real turn in a successor task. Fixtures that
plant ``_session_tasks`` directly never exercised that hop, so the outcome was decided from the
bounced first task and logged ``skipped:not-admitted`` for turns that went on to run.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionEntry
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _telegram_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="1"))
    adapter.send_typing = AsyncMock()
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True, message_id="1"))
    adapter._keep_typing = AsyncMock()
    adapter._stop_typing_refresh = AsyncMock()
    adapter._run_processing_hook = AsyncMock()
    return adapter


def _wire(monkeypatch, *, succeeded: bool):
    runner, adapter = make_restart_runner(_telegram_adapter())
    source = make_restart_source(chat_id="real-path-chat")
    key = runner._session_key_for_source(source)
    entry = SessionEntry(
        session_key=key, session_id="sid", created_at=datetime.now(), updated_at=datetime.now(),
        origin=source, platform=Platform.TELEGRAM, chat_type="dm", resume_pending=True,
        resume_reason="restart_interrupted", last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {key: entry}
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
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
    turns: list[str] = []

    async def run_turn(event, _source, quick_key, _generation):
        # The production turn marks failure first, then reports its real result after awaiting.
        event._agent_turn_succeeded = False
        turns.append(quick_key)
        await asyncio.sleep(0.05)
        event._agent_turn_succeeded = succeeded
        if succeeded:
            entry.resume_pending = False
        return "RESUMED OK"

    runner._handle_message_with_agent = run_turn
    adapter.set_message_handler(runner._handle_message)
    adapter.gateway_runner = runner
    tasks = []
    runner._retain_background_task = lambda task: tasks.append(task) or task
    monkeypatch.setattr(runner, "_auto_resume_ready", lambda _entry: (adapter, source), raising=False)
    return runner, adapter, entry, turns, tasks


def _outcomes(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]


@pytest.mark.asyncio
async def test_resume_bounced_by_startup_preclaim_is_logged_resumed_after_its_real_turn(monkeypatch, caplog):
    runner, adapter, entry, turns, tasks = _wire(monkeypatch, succeeded=True)
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry]) == 1
        # A reconnect scan while the resume runs must not admit a second resume turn.
        await asyncio.sleep(0)
        assert runner._schedule_resume_pending_sessions(candidates=[entry]) == 0
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        for _ in range(20):
            await asyncio.sleep(0.01)
    assert turns == [entry.session_key]
    assert _outcomes(caplog) == [f"Restart recovery outcome: session={entry.session_key} outcome=resumed"]
    runner._send_interrupted_turn_notes.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_bounced_by_startup_preclaim_reports_a_failed_real_turn(monkeypatch, caplog):
    runner, adapter, entry, turns, tasks = _wire(monkeypatch, succeeded=False)
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry]) == 1
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        for _ in range(20):
            await asyncio.sleep(0.01)
    assert turns == [entry.session_key]
    outcomes = _outcomes(caplog)
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=skipped:turn-failed"]
    runner._send_interrupted_turn_notes.assert_not_awaited()
