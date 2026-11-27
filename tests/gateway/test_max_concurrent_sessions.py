"""Tests for the gateway max_concurrent_sessions active-session cap."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


@pytest.fixture(autouse=True)
def _isolated_active_session_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}

    async def send(self, chat_id, text, **kwargs):
        return None

    async def interrupt_session_activity(self, session_key, chat_id):
        event = self._active_sessions.get(session_key)
        if event is not None:
            event.set()


def _make_source(chat_id: str = "chat-1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id=f"user-{chat_id}",
    )


def _make_event(text: str = "hello", chat_id: str = "chat-1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(chat_id),
    )


def _make_runner(max_concurrent_sessions: int | None = None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
        max_concurrent_sessions=max_concurrent_sessions,
    )
    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._busy_input_mode = "interrupt"
    runner._busy_text_mode = "interrupt"
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.delivery_router = MagicMock()
    return runner


def _occupy_session(runner: GatewayRunner, chat_id: str = "busy"):
    source = _make_source(chat_id)
    session_key = build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    runner._session_state(session_key).turn.started_ts = time.time()
    return session_key


def _silence_global_gateway_hooks(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: [])
    monkeypatch.setattr("tools.slash_confirm.get_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.slash_confirm.clear_if_stale", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.approval.has_blocking_approval", lambda *args, **kwargs: False)


def test_new_session_gets_clean_error_at_active_session_limit(monkeypatch):
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(max_concurrent_sessions=1)
    _occupy_session(runner, "busy")
    event = _make_event(chat_id="new")
    new_key = build_session_key(event.source)

    async def fail_if_agent_runs(self_inner, ev, src, qk, generation):
        raise AssertionError("_handle_message_with_agent should not run at capacity")

    with patch.object(GatewayRunner, "_handle_message_with_agent", fail_if_agent_runs):
        result = asyncio.run(runner._handle_message(event))

    assert "(1/1)" in result
    assert new_key not in runner._running_agents
    runner.session_store.get_or_create_session.assert_not_called()


def test_status_command_bypasses_active_session_limit(monkeypatch):
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(max_concurrent_sessions=1)
    _occupy_session(runner, "busy")
    runner._handle_status_command = AsyncMock(return_value="status ok")

    result = asyncio.run(runner._handle_message(_make_event("/status", chat_id="new")))

    assert result == "status ok"
    runner._handle_status_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_capped_plugin_injection_requeues_its_claim(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gateway import session_automation_retry as retry
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import list_session_admissions
    from tests.gateway.test_automation_notification_category import _native_owner

    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    ref = SessionRef(authority.profile_id, metadata['gateway_session_id'])
    try:
        with patch.object(runner, '_handle_message', AsyncMock(return_value=None)):
            await authority._drain(ref)  # Settle the fixture's human seed without a provider.
        runner.config.max_concurrent_sessions = 1
        _occupy_session(runner)
        monkeypatch.setattr(retry, 'time', SimpleNamespace(time=lambda: 1000.0))
        run_agent = AsyncMock(side_effect=AssertionError('the refused turn must not reach the agent'))
        monkeypatch.setattr(runner, '_handle_message_with_agent', run_agent)
        send = AsyncMock(side_effect=AssertionError('a machine turn must not post the limit notice'))
        monkeypatch.setattr(adapter, 'send', send)

        assert await runner._admit_plugin_injection(
            adapter, source, session_key=metadata['gateway_session_key'], session_id=ref.session_id,
            content='continue this handoff', plugin_id='relay',
        ) is True
        row, _ = await authority._claim_next(ref, authority.sessions[ref.session_id])
        outcome = await authority._execute_claimed(ref, authority.sessions[ref.session_id], row)
        assert outcome == (None, 'deferred')
        queued, = list_session_admissions(authority.db, session_id=ref.session_id)
        assert (queued['admission_id'], queued['status']) == (row['admission_id'], 'queued')
        assert retry.retry_state(authority.db, row['admission_id']) == {'attempt': 1, 'due_at': 1010.0}
        assert (await authority._claim_next(ref, authority.sessions[ref.session_id]))[0] == 'automation_wait'
        run_agent.assert_not_awaited()
        send.assert_not_awaited()
    finally:
        authority.db.close()


