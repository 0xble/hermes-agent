"""Legacy process checkpoints resolve their chat without inventing a topic."""

import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG, logging.DEBUG, logging.WARNING, logging.DEBUG]
