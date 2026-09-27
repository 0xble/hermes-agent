"""Explicit stop retires only the restart marker present when the stop began."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, SessionStore, build_session_key


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["busy", "idle", "other"])
@pytest.mark.parametrize("outcome", ["clear", "successor", "failure"])
async def test_stop_preserves_successor_marker(tmp_path, route, outcome, monkeypatch, caplog):
    from gateway.run import GatewayRunner

    source = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", chat_type="dm")
    key = build_session_key(source)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")})
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._running_agents = {key: MagicMock()}
    runner._pending_messages = {}
    runner._invalidate_session_run_generation = lambda *a, **kw: None
    runner._release_running_agent_state = lambda *a, **kw: None
    runner._is_user_authorized_for_source = lambda *a, **kw: True
    runner.session_store = SessionStore(tmp_path / "sessions", runner.config)
    runner.session_store.get_or_create_session(source)
    runner.session_store.mark_resume_pending(key, "old")
    if outcome == "failure":
        def fail_clear(*args, **kwargs):
            raise OSError("storage unavailable")
        monkeypatch.setattr(runner.session_store, "clear_resume_pending", fail_clear)

    class Adapter:
        send = AsyncMock()

        async def interrupt_session_activity(self, *args, **kwargs):
            if outcome == "successor":
                runner.session_store.mark_resume_pending(key, "successor")

    runner.adapters = {Platform.TELEGRAM: Adapter()}
    event = MessageEvent(text="/stop", message_type=MessageType.TEXT, source=source)
    if route == "busy":
        await runner._busy_stop_command(event, key, source)
    elif route == "idle":
        await runner._handle_stop_command(event)
    else:
        await runner._interrupt_and_clear_session(key, source, interrupt_reason="other", invalidation_reason="test")
    # Reload the durable routing entry, rather than just inspecting the mutable object.
    fresh = SessionStore(tmp_path / "sessions", runner.config).get_or_create_session(source)
    assert fresh.resume_pending is (outcome != "clear" or route == "other")
    if outcome == "successor":
        assert fresh.resume_reason == "successor"
    if outcome == "failure" and route != "other":
        assert "Could not persist restart marker clear" in caplog.text


def test_conditional_clear_distinguishes_repeated_marks_at_same_clock_time(tmp_path, monkeypatch):
    from datetime import datetime
    from gateway import session_lifecycle

    monkeypatch.setattr(session_lifecycle, "_now", lambda: datetime(2026, 9, 27))
    config = GatewayConfig()
    store = SessionStore(tmp_path / "sessions", config)
    source = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key)
    marker = store.get_resume_pending_marker(entry.session_key)
    store.mark_resume_pending(entry.session_key)
    assert store.clear_resume_pending(entry.session_key, expected_marker=marker) is False
    current = store.get_resume_pending_marker(entry.session_key)
    assert current != marker
    assert store.clear_resume_pending(entry.session_key, expected_marker=current) is True
    assert store.clear_resume_pending(entry.session_key, expected_marker=current) is False
