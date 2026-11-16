from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli.observability.request_usage_ledger import (
    REQUEST_ROW_KEY_SET,
    RequestUsageLedger,
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
