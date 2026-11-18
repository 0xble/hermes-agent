"""Legacy process checkpoints resolve their chat without inventing a topic."""

import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from tools import process_registry as processes


@pytest.fixture
def runner(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "display:\n  background_process_notifications: concise\n", encoding="utf-8")
    result = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    result._completion_notification_batch_window = 0
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_child_pin", [False, True])
async def test_recovered_bare_session_key_delivers_to_stored_origin(runner, monkeypatch, legacy_child_pin):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", thread_id="42")
    entry = runner.session_store.get_or_create_session(source)
    if legacy_child_pin:
        db = runner.session_store._db_for_key(entry.session_key)
        db.create_session("legacy-child", source="subagent", parent_session_id=entry.session_id,
                          model_config={"_delegate_from": entry.session_id})
    runner.session_store = SessionStore(runner.config.sessions_dir, runner.config)
    processes._checkpoint_path().write_text(json.dumps([{
        "session_id": "proc_legacy", "pid": os.getpid(), "session_key": entry.session_id,
        "parent_session_id": "legacy-child" if legacy_child_pin else entry.session_id,
        "watcher_platform": "telegram", "watcher_chat_id": "123", "watcher_thread_id": "",
        "watcher_interval": 5, "notify_on_complete": True, "command": "legacy worker",
        "owner_task_id": "parent-turn",
    }]), encoding="utf-8")
    registry = processes.ProcessRegistry()
    assert registry.recover_from_checkpoint() == 1
    monkeypatch.setattr(processes, "process_registry", registry)
    session = registry.get("proc_legacy")
    assert session is not None
    assert session.detached and session.session_key == entry.session_id
    # Recovery proved the live PID. Exercise the terminal watcher path without signaling it.
    session.exited, session.exit_code, session.output_buffer = True, 0, "RECOVERED_RESULT\n"
    watcher = registry.pending_watchers.pop()
    watcher["check_interval"] = 0

    async def admit(event):
        event._gateway_accepted = True

    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock(side_effect=admit), _active_sessions={})
    runner.adapters[Platform.TELEGRAM] = adapter
    await runner._run_process_watcher(watcher)
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_id == "123"
    assert event.source.thread_id == "42"
    assert event.source.chat_type == "dm"
    assert event.metadata["gateway_session_id"] == entry.session_id
    assert "RECOVERED_RESULT" in event.text
    adapter.send.assert_not_awaited()


@pytest.mark.parametrize("thread_id", ["", "17"])
def test_unknown_bare_key_uses_chat_type_but_never_another_topic(runner, thread_id):
    runner.session_store.get_or_create_session(SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", thread_id="42"))
    source = runner._build_process_event_source({
        "session_id": "proc_old", "session_key": "no-longer-indexed-session",
        "platform": "telegram", "chat_id": "123", "thread_id": thread_id,
    })
    assert source is not None
    assert source.chat_id == "123" and source.chat_type == "dm"
    assert source.thread_id == (thread_id or None)


def test_unknown_chat_does_not_borrow_type_from_another_chat(runner):
    runner.session_store.get_or_create_session(SessionSource(
        platform=Platform.TELEGRAM, chat_id="999", chat_type="dm"))
    assert runner._build_process_event_source({
        "session_id": "proc_unknown", "session_key": "missing", "platform": "telegram", "chat_id": "123",
    }) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["", "api_server"])
@pytest.mark.parametrize("event_type", ["async_delegation", "delegation_auto_resume", "completion"])
async def test_api_bound_completion_stays_on_raw_api_route(runner, owner, platform, event_type):
    from gateway.session_api import bind_api_session

    runner.session_store = owner.runner.session_store
    owner.runner = runner
    runner.session_authority = owner
    session_id = "api-bound-session"
    bind_api_session(owner, session_id)
    runner.adapters[Platform.API_SERVER] = SimpleNamespace(
        supports_async_delivery=False, _ensure_session_db=lambda: owner.db)
    entry = runner.session_store.lookup_by_session_id(session_id)
    assert entry.origin.platform == Platform.API_SERVER

    event = {"type": event_type, "session_key": session_id, "platform": platform}
    assert await runner._authorized_completion_notice_target(event, check_delivery_ready=True) == (None, True)
    # Messaging metadata permits the bare-id lookup, but an API origin cannot override it.
    source = runner._build_process_event_source({
        **event, "platform": "telegram", "chat_type": "dm", "chat_id": "123",
    })
    assert source.platform == Platform.TELEGRAM and source.chat_id == "123"


@pytest.mark.parametrize("other_profile", [False, True])
def test_chat_type_fallback_refuses_ambiguous_or_other_profile_origins(runner, other_profile):
    runner.session_store.get_or_create_session(SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", chat_type="dm",
        profile="secondary" if other_profile else None))
    if not other_profile:
        runner.session_store.get_or_create_session(SessionSource(
            platform=Platform.TELEGRAM, chat_id="123", chat_type="group"))
    assert runner._build_process_event_source({
        "session_id": "proc_ambiguous", "session_key": "missing", "platform": "telegram", "chat_id": "123",
    }) is None


def test_unresolvable_source_warns_once_per_process_across_ticks_and_completion(runner, caplog):
    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        for proc in ("proc_a", "proc_a", "proc_a", "proc_b"):
            assert runner._build_process_event_source({
                "session_id": proc, "session_key": "missing", "platform": "telegram", "chat_id": "123",
                "type": "completion" if proc == "proc_b" else "?",
            }) is None
        # A completion is a newly constructed event, not the watcher's dict.
        assert runner._build_process_event_source({
            "session_id": "proc_a", "session_key": "missing", "platform": "telegram", "chat_id": "123",
            "type": "completion", "profile": "default",
        }) is None
    records = [r for r in caplog.records if "Synthetic event source unresolvable" in r.message]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_unresolvable_source_warning_cache_evicts_oldest_without_refreshing_repeats(runner):
    import gateway.run_notifications_sources as sources

    def log_for(index):
        return runner._unresolvable_process_source_log({"session_id": f"proc_{index}"})

    for index in range(512):
        assert log_for(index) == sources.logger.warning
    assert log_for(0) == sources.logger.debug
    assert log_for(512) == sources.logger.warning
    assert len(runner._unresolvable_process_sources) == 512
    assert log_for(1) == sources.logger.debug
    assert log_for(0) == sources.logger.warning
    assert log_for(512) == sources.logger.debug
    assert len(runner._unresolvable_process_sources) == 512


def test_unresolved_process_route_retries_are_bounded_without_losing_recovery(runner, monkeypatch):
    import gateway.run_notifications_sources as sources

    now = [100.0]
    monkeypatch.setattr(sources.time, "monotonic", lambda: now[0])
    event = {"session_id": "proc_missing", "session_key": "missing", "platform": "telegram", "chat_id": "123"}
    with patch.object(runner.session_store, "list_sessions", wraps=runner.session_store.list_sessions) as scans:
        for _ in range(60):
            assert runner._build_process_event_source(dict(event)) is None
        assert scans.call_count == 1
        # Recovery still happens without a gateway restart, and the checkpoint is untouched.
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", thread_id="42")
        runner.session_store.get_or_create_session(source)
        recovered = runner._build_process_event_source(dict(event))
        assert recovered is not None and recovered.thread_id is None
        assert event["session_key"] == "missing"
        # An unchanged unresolved route is retried when the bounded delay expires.
        unknown = {**event, "session_id": "proc_other", "chat_id": "999"}
        assert runner._build_process_event_source(unknown) is None
        attempts = scans.call_count
        now[0] += 31
        assert runner._build_process_event_source(dict(unknown)) is None
        assert scans.call_count == attempts + 1
    with patch.object(runner, "_resolve_process_event_source", return_value=None):
        for index in range(520):
            runner._build_process_event_source({**event, "session_id": f"proc_{index}"})
    assert len(runner._process_source_retries) == 512
    assert event["session_id"] == "proc_missing"
