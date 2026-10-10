"""Gateway side of durable delegation retry: terminal lines bypass the parent model."""

import json
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.run import GatewayRunner
from tools import async_delegation as ad
from tools import delegation_resume as dr


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")


def _failed_row(did, failure_reason):
    ad._persist_dispatch({
        "delegation_id": did, "goal": "make the model", "goals": ["make the model"], "is_batch": True,
        "context": "c", "role": "leaf", "model": "m", "session_key": "agent:main:telegram:dm:1",
        "origin_ui_session_id": "", "origin_session_id": "", "parent_session_id": "parent-1",
        "dispatched_at": 1_800_000_000.0})
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='error', delivery_state='delivered', "
                     "completed_at=?, updated_at=?, event_json=?, "
                     "result_json=? WHERE delegation_id=?",
                     (1_800_000_060.0, 1_800_000_060.0, json.dumps({"status": "error"}),
                      json.dumps({"results": [{"task_index": 0, "status": "failed",
                                               "failure_reason": failure_reason, "error": "refused"}]}), did))


def _runner():
    runner = object.__new__(GatewayRunner)
    runner._completion_event_scope = lambda _evt: nullcontext()
    from types import SimpleNamespace
    from gateway.config import Platform

    runner._build_process_event_source = Mock(return_value=SimpleNamespace(platform=Platform.TELEGRAM))
    runner._delivery_adapter_for = Mock(return_value=object())
    runner._resolve_injection_adapter = Mock(return_value=object())
    runner._is_user_authorized_for_source = Mock(return_value=True)
    runner._deliver_platform_notice = AsyncMock(return_value=True)
    runner._inject_watch_notification = AsyncMock(side_effect=AssertionError("terminal must not wake the model"))
    runner._classify_completion_target = AsyncMock(return_value="deliver")
    runner._completion_delivery_ready = AsyncMock(return_value=True)
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("scrub_failure", [False, True])
async def test_terminal_report_scrubs_worker_errors_before_egress(monkeypatch, scrub_failure):
    import gateway.run as gr

    token = "synthetic-worker-token-abcdefghijklmnopqrstuvwxyz0123456789"
    _failed_row("deleg_secret_error", "unknown")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET result_json=? WHERE delegation_id=?",
                     (json.dumps({"error": f"Authorization: Bearer {token}"}), "deleg_secret_error"))
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    if scrub_failure:
        def broken_scrub(_text):
            raise RuntimeError("scrub unavailable")
        monkeypatch.setattr(gr, "_redact_gateway_user_facing_secrets", broken_scrub)
    await runner._deliver_delegation_retry_action(action)
    if scrub_failure:
        runner._deliver_platform_notice.assert_not_awaited()
        assert dr.retry_status("deleg_secret_error")["retry_state"] == "terminal"
    else:
        runner._deliver_platform_notice.assert_awaited_once()
        line = runner._deliver_platform_notice.await_args.args[1]
        assert token not in line
        assert "Authorization:" not in line  # categories, never raw exception text
        assert "make the model" in line and "\n" not in line


def test_terminal_goal_scrub_precedes_truncation(monkeypatch):
    from agent import redact

    scrub = Mock(wraps=redact.redact_for_egress)
    monkeypatch.setattr(redact, "redact_for_egress", scrub)
    token = "synthetic-goal-token-abcdefghijklmnopqrstuvwxyz0123456789"
    goal = "x" * 80 + f" Authorization: Bearer {token} finish the task"
    line = dr.build_terminal_line({"task": {"goal": goal}, "retry_reason": "billing",
                                   "delegation_id": "deleg_secret_goal"})
    assert "synthetic-goal" not in line  # a partial token must not survive the 120-char cut
    scrub.assert_called_once_with(goal)
    assert "billing" not in line  # a readable fixed category, not an internal reason code


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_error", [False, True])
async def test_revoked_terminal_target_is_settled_without_send(caplog, auth_error):
    _failed_row("deleg_revoked", "billing")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    runner._is_user_authorized_for_source = Mock(
        side_effect=RuntimeError("policy unavailable") if auth_error else None, return_value=False)
    await runner._deliver_delegation_retry_action(action)
    runner._is_user_authorized_for_source.assert_called_once()
    runner._deliver_platform_notice.assert_not_awaited()
    record = dr.retry_status("deleg_revoked")
    assert record["retry_state"] == "cancelled"
    assert record["retry_reason"] == "authorization_revoked"
    source = runner._build_process_event_source.return_value
    runner._resolve_injection_adapter.assert_called_once_with("telegram", source)
    runner._is_user_authorized_for_source.assert_called_once_with(source)
    assert dr.sweep_retries(now=1_800_100_000.0) == []
    assert "suppress" in caplog.text.lower()


@pytest.mark.asyncio
async def test_disconnected_terminal_transport_remains_owed():
    _failed_row("deleg_disconnected", "billing")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    runner._resolve_injection_adapter.return_value = None
    await runner._deliver_delegation_retry_action(action)
    runner._deliver_platform_notice.assert_not_awaited()
    runner._is_user_authorized_for_source.assert_not_called()
    assert dr.retry_status("deleg_disconnected")["retry_state"] == "terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["failed", "missing", "success"])
async def test_terminal_send_requires_real_adapter_receipt(receipt):
    from types import SimpleNamespace
    from gateway.config import Platform
    from gateway.platforms.base import SendResult

    _failed_row("deleg_adapter_receipt", "billing")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="1", user_id="u")
    adapter = SimpleNamespace(config=None, send=AsyncMock(return_value={
        "failed": SendResult(success=False, error="chat offline"),
        "missing": None, "success": SendResult(success=True, message_id="receipt-1")}[receipt]))
    runner._build_process_event_source.return_value = source
    runner._delivery_adapter_for.return_value = adapter
    runner._thread_metadata_for_source = Mock(return_value={})
    runner._deliver_platform_notice = GatewayRunner._deliver_platform_notice.__get__(runner)
    await runner._deliver_delegation_retry_action(action)
    adapter.send.assert_awaited_once()
    assert dr.retry_status("deleg_adapter_receipt")["retry_state"] == (
        "reported" if receipt == "success" else "terminal")


@pytest.mark.asyncio
async def test_skipped_terminal_send_remains_pending():
    _failed_row("deleg_skipped_receipt", "billing")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    runner._deliver_platform_notice = AsyncMock(return_value=False)
    await runner._deliver_delegation_retry_action(action)
    assert dr.retry_status("deleg_skipped_receipt")["retry_state"] == "terminal"


@pytest.mark.asyncio
async def test_repeated_failed_reports_remain_owed_until_success(monkeypatch):
    clock = [1_800_000_100.0]
    monkeypatch.setattr(dr.time, "time", lambda: clock[0])
    _failed_row("deleg_repeated_send", "billing")
    runner = _runner()
    runner._deliver_platform_notice = AsyncMock(return_value=False)
    for _ in range(12):
        (action,) = dr.sweep_retries(now=clock[0])
        await runner._deliver_delegation_retry_action(action)
        assert dr.retry_status("deleg_repeated_send")["retry_state"] == "terminal"
        assert dr.sweep_retries(now=clock[0] + 1) == []  # do not spin an unavailable transport
        clock[0] += 3601
    runner._deliver_platform_notice = AsyncMock(return_value=True)
    (action,) = dr.sweep_retries(now=clock[0])
    await runner._deliver_delegation_retry_action(action)
    assert dr.retry_status("deleg_repeated_send")["retry_state"] == "reported"
    assert dr.sweep_retries(now=clock[0] + 3601) == []


@pytest.mark.asyncio
async def test_terminal_failure_is_sent_to_user_once_without_model_turn():
    _failed_row("deleg_policy", "content_policy_blocked")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    await runner._deliver_delegation_retry_action(action)
    runner._deliver_platform_notice.assert_awaited_once()
    line = runner._deliver_platform_notice.await_args.args[1]
    assert line == "Background task stopped: make the model — provider rejected the request."
    assert dr.sweep_retries(now=1_800_100_000.0) == []


@pytest.mark.asyncio
async def test_failed_terminal_send_is_retried_later():
    _failed_row("deleg_send_fail", "format_error")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    runner._deliver_platform_notice = AsyncMock(side_effect=RuntimeError("chat offline"))
    await runner._deliver_delegation_retry_action(action)
    (again,) = dr.sweep_retries(now=1_800_000_200.0)
    assert again["kind"] == "terminal"


@pytest.mark.asyncio
async def test_retry_notice_for_closed_parent_becomes_terminal_line():
    _failed_row("deleg_closed", "rate_limit")
    plan = dr.retry_status("deleg_closed")
    dr.schedule_retry("deleg_closed", now=1_800_000_060.0)
    plan = dr.retry_status("deleg_closed")
    (action,) = dr.sweep_retries(now=plan["retry_due_at"] + 1)
    assert action["kind"] == "notice"
    runner = _runner()
    runner._classify_completion_target = AsyncMock(return_value="terminal")
    await runner._deliver_delegation_retry_action(action)
    (terminal,) = dr.sweep_retries(now=plan["retry_due_at"] + 2)
    assert terminal["kind"] == "terminal" and "conversation ended" in terminal["text"]
    runner._completion_delivery_ready.return_value = False
    await runner._deliver_delegation_retry_action(terminal)
    runner._deliver_platform_notice.assert_awaited_once()
    assert dr.retry_status("deleg_closed")["retry_state"] == "reported"


@pytest.mark.asyncio
async def test_retry_notice_injects_parent_turn_and_starts_grace():
    _failed_row("deleg_notice", "rate_limit")
    dr.schedule_retry("deleg_notice", now=1_800_000_060.0)
    due = dr.retry_status("deleg_notice")["retry_due_at"]
    (action,) = dr.sweep_retries(now=due + 1)
    runner = _runner()
    runner._inject_watch_notification = AsyncMock(return_value=True)
    await runner._deliver_delegation_retry_action(action)
    runner._inject_watch_notification.assert_awaited_once()
    assert dr.retry_status("deleg_notice")["retry_state"] == "notified"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_error", [False, True])
async def test_revoked_retry_notice_target_never_wakes_parent(caplog, auth_error):
    """Internal wakes bypass inbound authorization, so a revoked origin must not get recovery work."""
    _failed_row("deleg_notice_revoked", "rate_limit")
    dr.schedule_retry("deleg_notice_revoked", now=1_800_000_060.0)
    due = dr.retry_status("deleg_notice_revoked")["retry_due_at"]
    (action,) = dr.sweep_retries(now=due + 1)
    assert action["kind"] == "notice"
    runner = _runner()
    runner._is_user_authorized_for_source = Mock(
        side_effect=RuntimeError("policy unavailable") if auth_error else None, return_value=False)
    runner._inject_watch_notification = AsyncMock(return_value=True)
    await runner._deliver_delegation_retry_action(action)
    runner._is_user_authorized_for_source.assert_called_once()
    runner._inject_watch_notification.assert_not_awaited()
    record = dr.retry_status("deleg_notice_revoked")
    assert record["retry_state"] == "cancelled"
    assert record["retry_reason"] == "authorization_revoked"
    assert dr.sweep_retries(now=due + 100_000) == []
    assert "suppress" in caplog.text.lower()


@pytest.mark.asyncio
async def test_disconnected_retry_notice_target_remains_scheduled():
    _failed_row("deleg_notice_offline", "rate_limit")
    dr.schedule_retry("deleg_notice_offline", now=1_800_000_060.0)
    due = dr.retry_status("deleg_notice_offline")["retry_due_at"]
    (action,) = dr.sweep_retries(now=due + 1)
    runner = _runner()
    runner._resolve_injection_adapter.return_value = None
    runner._inject_watch_notification = AsyncMock(return_value=True)
    await runner._deliver_delegation_retry_action(action)
    runner._inject_watch_notification.assert_not_awaited()
    runner._is_user_authorized_for_source.assert_not_called()
    assert dr.retry_status("deleg_notice_offline")["retry_state"] == "scheduled"


@pytest.mark.asyncio
async def test_retry_sweep_and_delivery_keep_secondary_profile_ledger_isolated(tmp_path, monkeypatch):
    """A/B/A sweeps and acknowledgements resolve each row in its owning profile."""
    from pathlib import Path
    from types import SimpleNamespace
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    secondary = root / "profiles" / "b"
    root.mkdir(parents=True)
    secondary.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(ad, "_db_path", lambda: get_hermes_home() / "state.db")
    for home, reason in [(root, "rate_limit"), (secondary, "billing")]:
        token = set_hermes_home_override(home)
        try:
            _failed_row("deleg_same_id", reason)
            dr.schedule_retry("deleg_same_id", now=1_800_000_060.0)
        finally:
            reset_hermes_home_override(token)
    runner = _runner()
    runner._served_profile_homes = {"default": root, "b": secondary}
    monkeypatch.setattr(dr.time, "time", lambda: 1_800_000_400.0)
    runner._build_process_event_source = lambda event: SimpleNamespace(profile=event["profile"], platform="telegram")
    runner._resolve_profile_home_for_source = lambda source: secondary if source.profile == "b" else root
    runner._completion_event_scope = GatewayRunner._completion_event_scope.__get__(runner)
    runner._inject_watch_notification = AsyncMock(return_value=True)

    assert get_hermes_home() == root
    actions = runner._collect_delegation_retry_actions()
    assert {(a["profile"], a["kind"]) for a in actions} == {("default", "notice"), ("b", "terminal")}
    assert get_hermes_home() == root
    runner._collect_delegation_retry_actions = lambda: actions
    await runner._drive_delegation_retries()
    runner._inject_watch_notification.assert_awaited_once()
    runner._deliver_platform_notice.assert_awaited_once()
    assert dr.retry_status("deleg_same_id")["retry_state"] == "notified"
    token = set_hermes_home_override(secondary)
    try:
        assert dr.retry_status("deleg_same_id")["retry_state"] == "reported"
    finally:
        reset_hermes_home_override(token)
    assert get_hermes_home() == root
