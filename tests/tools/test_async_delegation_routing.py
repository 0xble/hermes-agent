"""Detached delegation completions retain the originating message route."""

import queue

from gateway.session_context import reset_session_vars, set_session_vars
from tools import async_delegation as delegation
from tools.process_registry import process_registry


def _record():
    return {
        "delegation_id": "deleg_route_test",
        "session_key": "agent:main:slack:dm:D0TEST",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": "parent",
        "goal": "route this result",
        "status": "completed",
        "is_batch": False,
        "model": "test-model",
    }


def test_slack_thread_route_is_persisted_on_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    set_session_vars(
        platform="slack", chat_type="dm", chat_id="D0TEST", thread_id="1795210800.890719",
        message_id="1795210800.890719", scope_id="TTEST",
    )
    try:
        record = _record()
        record.update(delegation._capture_routing_origin())
        delegation._push_completion_event(record, {"summary": "done", "duration_seconds": 0.1}, "completed")
    finally:
        reset_session_vars()

    event = q.get_nowait()
    assert event["platform"] == "slack"
    assert event["chat_id"] == "D0TEST"
    assert event["thread_id"] == "1795210800.890719"


def test_top_level_route_keeps_thread_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    set_session_vars(platform="slack", chat_type="dm", chat_id="D0TEST", thread_id="")
    try:
        record = _record()
        record.update(delegation._capture_routing_origin())
        delegation._push_completion_event(record, {"summary": "done", "duration_seconds": 0.1}, "completed")
    finally:
        reset_session_vars()

    event = q.get_nowait()
    assert event["platform"] == "slack"
    assert event["chat_id"] == "D0TEST"
    assert event["thread_id"] == ""
