from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli.observability.request_usage_ledger import (
    REQUEST_ROW_KEY_SET,
    RequestUsageLedger,
    _auxiliary_row,
    note_attended_switch,
    row_for_attended_switch,
    row_for_error,
    row_for_response,
)


def _agent(**kwargs):
    values = dict(
        session_id="session-1", platform="cli", provider="openai", model="gpt-5",
        requested_provider="openai", _fallback_index=0, _fallback_activated=False,
        _primary_runtime={"provider": "openai", "requested_provider": "openai", "model": "gpt-5"},
        _cached_system_prompt_parts={"stable": "abc", "context": "de", "volatile": "f"},
        _cached_system_prompt="abc\n\nde\n\nf",
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_success_row_has_exact_size_and_route_schema_without_content():
    row = row_for_response(_agent(), {
        "input_tokens": 101, "cache_read_tokens": 20,
        "cache_write_tokens": 3, "output_tokens": 44,
    })
    assert set(row) == REQUEST_ROW_KEY_SET
    assert row["resolved_provider"] == "openai"
    assert row["resolved_model"] == "gpt-5"
    assert row["input_tokens"] == 101
    assert row["system_prompt_tier_sizes"] == {"stable": 3, "context": 2, "volatile": 1}
    assert not any(token in json.dumps(row["system_prompt_tier_sizes"]).lower() for token in ("message", "tool", "secret"))


def test_fallback_row_preserves_requested_route_and_index():
    row = row_for_response(_agent(
        provider="anthropic", model="claude-sonnet", requested_provider="anthropic",
        _fallback_index=2, _fallback_activated=True,
        _primary_runtime={"provider": "openai", "requested_provider": "openai", "model": "gpt-5"},
    ))
    assert set(row) == REQUEST_ROW_KEY_SET
    assert row["requested_provider"] == "openai"
    assert row["requested_model"] == "gpt-5"
    assert row["resolved_provider"] == "anthropic"
    assert row["resolved_model"] == "claude-sonnet"
    assert row["fallback_index"] == 1


def test_failed_row_sets_failure_class_and_has_no_error_content():
    row = row_for_error(_agent(), "rate_limit")
    assert set(row) == REQUEST_ROW_KEY_SET
    assert row["failure_class"] == "rate_limit"
    assert "error" not in row
    assert "message" not in row


def test_write_failure_is_swallowed_and_counted(tmp_path):
    ledger = RequestUsageLedger(tmp_path)
    ledger._append = lambda root, row: (_ for _ in ()).throw(OSError("read-only"))
    assert ledger.record(row_for_error(_agent(), "network")) is True
    assert ledger.flush()
    assert ledger.error_count == 1


def test_default_ledger_exit_drain_is_bounded(monkeypatch):
    from hermes_cli.observability import request_usage_ledger as module

    flush = SimpleNamespace(flush=lambda **kwargs: calls.append(kwargs))
    calls = []
    monkeypatch.setattr(module, "_default_ledger", flush)
    module._flush_default_ledger_at_exit()
    assert calls == [{"timeout": 2.0}]


def test_attended_switch_is_a_separate_row_with_route_and_idle_bucket():
    row = row_for_attended_switch(
        session_id="session-1", source="gateway", from_model="fast", to_model="smart",
        idle_gap_bucket="15m_to_1h",
    )
    assert set(row) == REQUEST_ROW_KEY_SET
    assert row["role_task_label"] == "attended_model_switch"
    assert row["from_model"] == "fast"
    assert row["to_model"] == "smart"
    assert row["idle_gap_bucket"] == "15m_to_1h"
    assert row["failure_class"] is None


def test_ledger_read_filters_by_source_model_and_job(tmp_path):
    ledger = RequestUsageLedger(tmp_path)
    row = row_for_response(_agent())
    row["source"] = "cron"
    row["cron_job_id"] = "job-1"
    assert ledger.record(row)
    assert ledger.flush()
    assert len(ledger.read(source="cron", model="gpt-5", cron_job_id="job-1")) == 1
    assert ledger.read(source="gateway") == []


def test_incognito_scope_persists_no_row(tmp_path):
    from agent.incognito import incognito_scope
    ledger = RequestUsageLedger(tmp_path)
    with incognito_scope():
        assert ledger.record(row_for_response(_agent())) is False
    assert ledger.flush()
    assert ledger.read() == []
    assert not list(tmp_path.rglob("*.jsonl"))


def test_registered_incognito_session_persists_no_row(tmp_path):
    from agent.incognito import register_session
    ledger = RequestUsageLedger(tmp_path)
    register_session("private-1")
    try:
        assert ledger.record(row_for_response(_agent(session_id="private-1"))) is False
    finally:
        register_session("private-1", enabled=False)
    assert ledger.record(row_for_response(_agent(session_id="public-1"))) is True
    assert ledger.flush()
    assert [row["session_id"] for row in ledger.read()] == ["public-1"]


def test_record_response_usage_calls_note_response_with_nonzero_usage(monkeypatch):
    from agent import turn_usage
    from run_agent import AIAgent

    captured = []
    monkeypatch.setattr(
        "hermes_cli.observability.request_usage_ledger.note_response",
        lambda agent, usage=None: captured.append((agent, usage)) or True,
    )
    agent = AIAgent(
        api_key="k", base_url="https://openrouter.ai/api/v1", provider="openrouter",
        api_mode="chat_completions", model="openai/gpt-5.4-mini", session_id="response-session",
        platform="cli", quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False,
    )
    try:
        usage = SimpleNamespace(
            prompt_tokens=120, completion_tokens=9, total_tokens=129,
            prompt_tokens_details=SimpleNamespace(cached_tokens=20),
            completion_tokens_details=None,
        )
        turn_usage.record_response_usage(
            agent, SimpleNamespace(usage=usage, model=getattr(agent, "model", "")),
            messages=[{"role": "user", "content": "hello"}], api_call_count=1,
            api_duration=0.01, compression_attempts=0, max_compression_attempts=3,
        )
    finally:
        agent.close()
    assert len(captured) == 1
    assert captured[0][0].session_id == "response-session"
    assert captured[0][1].output_tokens == 9


def test_restored_prompt_uses_total_size_fallback():
    row = row_for_response(_agent(_cached_system_prompt_parts=None, _cached_system_prompt="abcdef"))
    assert row["system_prompt_tier_sizes"] == {"total": 6}


def test_stale_prompt_parts_use_total_size_fallback():
    row = row_for_response(_agent(
        _cached_system_prompt_parts={"stable": "abc", "context": "de", "volatile": "f"},
        _cached_system_prompt="prompt was reassigned directly",
    ))
    assert row["system_prompt_tier_sizes"] == {"total": 30}


def test_auxiliary_row_reads_canonical_usage_buckets():
    row = _auxiliary_row({
        "session_id": "s1", "platform": "cli", "aux_task": "title",
        "provider": "openai", "model": "gpt-5",
        "usage": {
            "input_tokens": 78, "cache_read_tokens": 20,
            "cache_write_tokens": 3, "output_tokens": 44,
            "prompt_tokens": 101, "total_tokens": 145,
        },
    })
    assert row["input_tokens"] == 78
    assert row["cache_read_tokens"] == 20
    assert row["cache_write_tokens"] == 3
    assert row["output_tokens"] == 44


def test_auxiliary_row_uses_cron_context_source_and_job_id():
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(session_id="cron_nightly", cron_session="1")
    try:
        row = _auxiliary_row({
            "session_id": "cron_nightly", "platform": "", "aux_task": "title",
            "provider": "openai", "model": "gpt-5", "usage": {},
        })
    finally:
        clear_session_vars(tokens)
    assert row["source"] == "cron"
    assert row["cron_job_id"] == "nightly"


def test_attended_switch_uses_agent_session_and_destination_model(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "hermes_cli.observability.request_usage_ledger.get_request_usage_ledger",
        lambda: SimpleNamespace(record=lambda row: captured.append(row) or True),
    )
    agent = _agent(session_id="agent-session", platform="cli", model="fast")
    runner = SimpleNamespace(_session_state=lambda _key: SimpleNamespace(
        conversation=SimpleNamespace(last_user_activity_at=None)))
    ctx = SimpleNamespace(session_key="routing-key")
    previous = SimpleNamespace(model="fast")
    assert note_attended_switch(runner, ctx, agent, previous, "smart")
    assert captured[0]["session_id"] == "agent-session"
    assert captured[0]["resolved_model"] == "smart"


def test_tier_sizes_match_the_real_system_prompt_join():
    from agent.system_prompt import build_system_prompt
    parts = {"stable": "You are Hermes.", "context": "", "volatile": "Today is Monday."}
    agent = _agent(_cached_system_prompt_parts=None, _cached_system_prompt=None)
    agent._emit_diagnostic_status = lambda *_: None
    import agent.system_prompt as sp
    original = sp.build_system_prompt_parts
    sp.build_system_prompt_parts = lambda *_a, **_k: dict(parts)
    try:
        agent._cached_system_prompt = build_system_prompt(agent)
    finally:
        sp.build_system_prompt_parts = original
    row = row_for_response(agent)
    assert row["system_prompt_tier_sizes"] == {"stable": 15, "context": 0, "volatile": 16}


def test_default_ledger_is_recreated_after_fork(monkeypatch):
    import hermes_cli.observability.request_usage_ledger as module
    first = module.get_request_usage_ledger()
    assert module.get_request_usage_ledger() is first
    monkeypatch.setattr(module.os, "getpid", lambda: -1)
    assert module.get_request_usage_ledger() is not first
