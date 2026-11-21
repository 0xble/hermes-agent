"""Committed context rewrites are attributed locally to the first eligible request."""

import json
import logging
import time
from types import SimpleNamespace

import pytest

from agent import compaction_events
from agent.incognito import incognito_scope
from hermes_cli.observability import request_usage_ledger as usage


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    store = usage.RequestUsageLedger(tmp_path / "request_usage")
    monkeypatch.setattr(usage, "get_request_usage_ledger", lambda: store)
    monkeypatch.setattr("agent.relay_compaction.compaction_target_available", lambda *a, **kw: False)
    monkeypatch.setattr("agent.relay_compaction.emit_compaction_mark", lambda *a, **kw: False)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: False)
    return store


def _agent(session_id="live", **kwargs):
    return SimpleNamespace(session_id=session_id, platform="cli", provider="openai", model="gpt-test", **kwargs)


def _prune(agent):
    before = [{"role": "tool", "content": "PRIVATE TOOL OUTPUT " * 100}]
    after = [{"role": "tool", "content": "Pruned"}]
    compaction_events.publish_prune(agent, before, after, 1)
    return compaction_events.estimate_messages_tokens_rough(before) - compaction_events.estimate_messages_tokens_rough(after)


def _flags(rows):
    return [(row["prune_committed"], row["compaction_committed"]) for row in rows]


def test_prune_logs_size_only_and_marks_first_failed_request_not_retry(ledger, caplog):
    agent = _agent()
    with caplog.at_level(logging.INFO, logger="agent.compaction_events"):
        reclaim = _prune(agent)
    assert [record.getMessage() for record in caplog.records if record.name == "agent.compaction_events"] == [
        f"Proactive tool-result prune committed: tokens_reclaimed={reclaim} tool_results_pruned=1",
    ]
    agent._current_api_request_started_at = time.time()
    assert usage.note_error(agent, "rate_limit")
    assert usage.note_auxiliary({"session_id": agent.session_id, "aux_task": "compression"})
    agent._current_api_request_started_at = time.time()
    assert usage.note_response(agent, {"cache_read_tokens": 42})

    failed, auxiliary, retry = ledger.read()
    assert _flags((failed, auxiliary, retry)) == [(True, False), (None, None), (False, False)]
    assert failed["failure_class"] == "rate_limit"
    assert retry["cache_read_tokens"] == 42
    assert "PRIVATE TOOL OUTPUT" not in json.dumps((failed, auxiliary, retry))


def test_rotation_and_inflight_failure_only_mark_later_request(ledger):
    agent = _agent("rotated-child", _inflight_turn_session_id="old-parent")
    old_request_started_at = time.time() - 1
    compaction_events.publish_attempt(agent, {
        "session_id": "old-parent", "route": "hermes", "commit_status": "aborted", "history_rewritten": True,
    })
    # An auxiliary summarizer or a request that predated the rewrite cannot consume it.
    assert usage.note_auxiliary({"session_id": agent.session_id, "aux_task": "compression"})
    agent._current_api_request_started_at = old_request_started_at
    assert usage.note_error(agent, "context_overflow")
    assert usage.note_response(_agent("old-parent", _current_api_request_started_at=time.time()))
    agent._current_api_request_started_at = time.time()
    assert usage.note_response(agent)
    assert usage.note_response(agent)
    assert _flags(ledger.read()) == [(None, None), (False, False), (False, False), (False, True), (False, False)]


def test_only_committed_hermes_rewrites_mark_and_two_kinds_can_coexist(ledger):
    agent = _agent()
    for state in ("blocked", "skipped", "failed", "aborted"):
        compaction_events.publish_attempt(agent, {"session_id": agent.session_id, "route": "hermes", "commit_status": state})
    compaction_events.publish_attempt(agent, {
        "session_id": agent.session_id, "route": "codex_app_server", "commit_status": "committed",
    })
    compaction_events.publish_provider_native(agent)
    compaction_events.publish_micro({"session_id": agent.session_id, "outcome": "stale_generation"})
    agent._current_api_request_started_at = time.time()
    assert usage.note_response(agent)
    compaction_events.publish_micro({"session_id": agent.session_id, "outcome": "defrag"})
    _prune(agent)
    agent._current_api_request_started_at = time.time()
    assert usage.note_response(agent)
    assert _flags(ledger.read()) == [(False, False), (True, True)]


def test_incognito_does_not_log_or_stage_a_rewrite(ledger, caplog):
    agent = _agent()
    with incognito_scope(session_id=agent.session_id), caplog.at_level(logging.INFO, logger="agent.compaction_events"):
        _prune(agent)
        compaction_events.publish_micro({"session_id": agent.session_id, "outcome": "absorbed"})
        agent._current_api_request_started_at = time.time()
        assert not usage.note_response(agent)
    assert not any("Proactive tool-result prune committed" in msg for msg in caplog.messages)
    assert usage.note_response(agent)
    assert _flags(ledger.read()) == [(False, False)]


def test_old_rows_and_invalid_flag_values_are_not_reported_as_observations():
    assert _flags([usage.sanitize_request_row({})]) == [(None, None)]
    assert _flags([usage.sanitize_request_row({"prune_committed": "true", "compaction_committed": 1})]) == [(None, None)]
