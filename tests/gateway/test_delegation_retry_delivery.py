"""Gateway side of durable delegation retry.

Routine terminal stops settle quietly. Only a completed result whose parent session is gone is sent
directly to the user, bypassing the parent model."""

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


def _undeliverable_row(did, parent="parent-1"):
    """A completed result whose durable completion delivery was permanently dropped."""
    _failed_row(did, "unknown")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='completed', delivery_state='dropped', parent_session_id=?, "
                     "result_json=?, event_json=? WHERE delegation_id=?",
                     (parent, json.dumps({"status": "completed", "results": [
                         {"task_index": 0, "status": "completed", "summary": "done"}]}),
                      json.dumps({"status": "completed"}), did))
    assert dr.schedule_retry(did, now=1_800_000_070.0) is None


def _gone_runner():
    runner = _runner()
    runner._classify_completion_target = AsyncMock(return_value="terminal")
    return runner


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
    _undeliverable_row("deleg_secret_error")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?",
                     (json.dumps({"goal": f"make the model Authorization: Bearer {token}"}), "deleg_secret_error"))
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _gone_runner()
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
        assert "make the model" in line and "\n" not in line


def test_terminal_goal_scrub_precedes_truncation(monkeypatch):
    from agent import redact

    scrub = Mock(wraps=redact.redact_for_egress)
    monkeypatch.setattr(redact, "redact_for_egress", scrub)
    token = "synthetic-goal-token-abcdefghijklmnopqrstuvwxyz0123456789"
    goal = "x" * 80 + f" Authorization: Bearer {token} finish the task"
    line = dr.build_terminal_line({"task": {"goal": goal}, "retry_reason": dr.RESULT_UNDELIVERABLE,
                                   "delegation_id": "deleg_secret_goal"})
    assert "synthetic-goal" not in line  # a partial token must not survive the 120-char cut
    scrub.assert_called_once_with(goal)
    assert dr.RESULT_UNDELIVERABLE not in line  # fixed wording, not an internal reason code


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_error", [False, True])
async def test_revoked_terminal_target_is_settled_without_send(caplog, auth_error):
    _undeliverable_row("deleg_revoked")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _gone_runner()
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
    _undeliverable_row("deleg_disconnected")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _gone_runner()
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

    _undeliverable_row("deleg_adapter_receipt")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _gone_runner()
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
    _undeliverable_row("deleg_skipped_receipt")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _gone_runner()
    runner._deliver_platform_notice = AsyncMock(return_value=False)
    await runner._deliver_delegation_retry_action(action)
    assert dr.retry_status("deleg_skipped_receipt")["retry_state"] == "terminal"


@pytest.mark.asyncio
async def test_repeated_failed_reports_remain_owed_until_success(monkeypatch):
    clock = [1_800_000_100.0]
    monkeypatch.setattr(dr.time, "time", lambda: clock[0])
    _undeliverable_row("deleg_repeated_send")
    runner = _gone_runner()
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


def _budget_exhausted(did):
    _failed_row(did, "rate_limit")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET retry_attempt=? WHERE delegation_id=?", (dr.MAX_RETRY_ATTEMPTS, did))


def _age_exhausted(did):
    _failed_row(did, "rate_limit")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET retry_root_started_at=? WHERE delegation_id=?",
                     (1_800_000_000.0 - dr.MAX_LINEAGE_AGE_S, did))


def _not_dispatched(did):
    _failed_row(did, "rate_limit")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET retry_state='terminal', retry_reason=? WHERE delegation_id=?",
                     ("the parent did not dispatch the retry (rate_limit)", did))


def _conversation_ended(did):
    _failed_row(did, "rate_limit")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET retry_state='terminal', retry_reason=? WHERE delegation_id=?",
                     ("its conversation ended before the retry could run", did))


ROUTINE_STOPS = {
    **{reason: (lambda did, r=reason: _failed_row(did, r)) for reason in (
        "content_policy_blocked", "billing", "auth_permanent", "format_error", "context_length_exceeded", "unknown")},
    "attempt_budget": _budget_exhausted,
    "age_budget": _age_exhausted,
    "parent_skipped_retry": _not_dispatched,
    "conversation_ended": _conversation_ended,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["deliver", "terminal", "retry"])
@pytest.mark.parametrize("category", sorted(ROUTINE_STOPS))
async def test_routine_terminal_stop_is_settled_without_notice_or_model_turn(category, parent):
    did = f"deleg_quiet_{category}"
    ROUTINE_STOPS[category](did)
    dr.schedule_retry(did, now=1_800_000_070.0)
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    assert action["kind"] == "terminal" and action["text"] == ""
    runner = _runner()
    runner._classify_completion_target = AsyncMock(return_value=parent)
    await runner._deliver_delegation_retry_action(action)
    runner._deliver_platform_notice.assert_not_awaited()
    runner._inject_watch_notification.assert_not_awaited()
    runner._is_user_authorized_for_source.assert_not_called()
    record = dr.retry_status(did)
    assert record["retry_state"] == "reported" and record["retry_reason"]  # durable cause retained
    assert dr.sweep_retries(now=1_800_100_000.0) == []


@pytest.mark.asyncio
async def test_crashed_routine_report_claim_replays_quietly():
    """A claim left in 'reporting' by an old build or crash is reclaimed and settled without a send."""
    _failed_row("deleg_quiet_replay", "content_policy_blocked")
    dr.schedule_retry("deleg_quiet_replay", now=1_800_000_070.0)
    (lost,) = dr.sweep_retries(now=1_800_000_100.0)
    assert dr.retry_status("deleg_quiet_replay")["retry_state"] == "reporting"
    (action,) = dr.sweep_retries(now=1_800_000_100.0 + dr.RETRY_CLAIM_TTL_S + 1)
    assert action["claim"] != lost["claim"] and action["text"] == ""
    runner = _runner()
    await runner._deliver_delegation_retry_action(action)
    await runner._deliver_delegation_retry_action(lost)  # stale claim is a no-op
    runner._deliver_platform_notice.assert_not_awaited()
    assert dr.retry_status("deleg_quiet_replay")["retry_state"] == "reported"


@pytest.mark.asyncio
async def test_undeliverable_result_with_live_parent_is_settled_quietly():
    _undeliverable_row("deleg_undeliverable_live")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    assert action["kind"] == "terminal" and action["text"]
    runner = _runner()
    await runner._deliver_delegation_retry_action(action)
    runner._classify_completion_target.assert_awaited_once_with("parent-1", settle_ended_tip=True)
    runner._deliver_platform_notice.assert_not_awaited()
    runner._is_user_authorized_for_source.assert_not_called()
    assert dr.retry_status("deleg_undeliverable_live")["retry_state"] == "reported"


@pytest.mark.asyncio
async def test_undeliverable_result_with_uncertain_parent_remains_owed(monkeypatch):
    monkeypatch.setattr(dr.time, "time", lambda: 1_800_000_100.0)
    _undeliverable_row("deleg_undeliverable_uncertain")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    runner._classify_completion_target = AsyncMock(return_value="retry")
    await runner._deliver_delegation_retry_action(action)
    runner._deliver_platform_notice.assert_not_awaited()
    assert dr.retry_status("deleg_undeliverable_uncertain")["retry_state"] == "terminal"
    assert dr.sweep_retries(now=1_800_000_101.0) == []  # bounded backoff, no spin


@pytest.mark.asyncio
@pytest.mark.parametrize("end_reason", ["missing", "new_session", "user_exit"])
async def test_undeliverable_result_with_gone_parent_is_sent_once_without_model_turn(end_reason):
    """Exercise the real parent classifier: missing or user-closed parent sessions are gone."""
    from types import SimpleNamespace

    _undeliverable_row("deleg_undeliverable_gone")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    parent = None if end_reason == "missing" else {"id": "parent-1", "ended_at": 1.0, "end_reason": end_reason}
    runner._session_db = SimpleNamespace(get_session=AsyncMock(return_value=parent))
    runner._classify_completion_target = GatewayRunner._classify_completion_target.__get__(runner)
    await runner._deliver_delegation_retry_action(action)
    runner._is_user_authorized_for_source.assert_called_once()
    runner._deliver_platform_notice.assert_awaited_once()
    line = runner._deliver_platform_notice.await_args.args[1]
    assert line == "Background task finished: make the model — its result could not be delivered."
    assert dr.retry_status("deleg_undeliverable_gone")["retry_state"] == "reported"
    assert dr.sweep_retries(now=1_800_100_000.0) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end_reason", ["", "compression_live", "idle"])
async def test_undeliverable_result_with_routable_parent_uses_real_classifier_quietly(end_reason):
    from types import SimpleNamespace

    _undeliverable_row("deleg_undeliverable_routable")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _runner()
    sessions = {"parent-1": {"id": "parent-1", "ended_at": None if not end_reason else 1.0,
                             "end_reason": "compression" if end_reason == "compression_live" else end_reason},
                "tip-1": {"id": "tip-1", "ended_at": None}}
    runner._session_db = SimpleNamespace(get_session=AsyncMock(side_effect=lambda sid: sessions.get(sid)),
                                         get_compression_tip=AsyncMock(return_value="tip-1"))
    runner._classify_completion_target = GatewayRunner._classify_completion_target.__get__(runner)
    await runner._deliver_delegation_retry_action(action)
    runner._deliver_platform_notice.assert_not_awaited()
    assert dr.retry_status("deleg_undeliverable_routable")["retry_state"] == "reported"


@pytest.mark.asyncio
@pytest.mark.parametrize("tip_end", [
    "new_session", "user_exit", "session_switch", "idle",
    "compression", "missing_tip", "rotation", "db_unavailable",
])
async def test_undeliverable_result_after_compressed_parent_settles_or_retries(monkeypatch, tip_end):
    """An ended continuation is not necessarily an in-flight compression rotation."""
    from types import SimpleNamespace

    monkeypatch.setattr(dr.time, "time", lambda: 1_800_000_100.0)
    did = "deleg_compressed_terminal"
    _undeliverable_row(did)
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    sessions = {
        "parent-1": {"ended_at": 1.0, "end_reason": "compression"},
        "tip-1": {"ended_at": 2.0, "end_reason": tip_end},
    }
    if tip_end == "missing_tip":
        sessions.pop("tip-1")
    runner = _runner()
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(side_effect=lambda sid: sessions.get(sid)),
        get_compression_tip=AsyncMock(
            return_value="parent-1" if tip_end == "rotation" else "tip-1",
            side_effect=RuntimeError("DB unavailable") if tip_end == "db_unavailable" else None),
    )
    runner._classify_completion_target = GatewayRunner._classify_completion_target.__get__(runner)
    await runner._deliver_delegation_retry_action(action)
    sends = runner._deliver_platform_notice.await_count
    record = dr.retry_status(did)
    again = dr.sweep_retries(now=1_800_100_000.0)
    print(f"{tip_end}: sends={sends}, retry_state={record['retry_state']}, reowed={bool(again)}")
    uncertain = tip_end in {"compression", "missing_tip", "rotation", "db_unavailable"}
    assert sends == (1 if tip_end in {"new_session", "user_exit", "session_switch"} else 0)
    runner._inject_watch_notification.assert_not_awaited()
    if uncertain:
        assert record["retry_state"] == "terminal"
        assert len(again) == 1 and again[0]["kind"] == "terminal"
    else:
        assert record["retry_state"] == "reported"
        assert again == []
    if sends:
        assert runner._deliver_platform_notice.await_args.args[1] == (
            "Background task finished: make the model — its result could not be delivered.")


@pytest.mark.asyncio
@pytest.mark.parametrize("tip_end", ["new_session", "user_exit", "session_switch", "idle"])
async def test_async_completion_with_ended_compression_tip_still_waits_for_live_tip(tip_end):
    """Terminal-report policy must not falsely acknowledge an ordinary completion."""
    from types import SimpleNamespace

    runner = _runner()
    sessions = {"parent-1": {"ended_at": 1.0, "end_reason": "compression"},
                "tip-1": {"ended_at": 2.0, "end_reason": tip_end}}
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(side_effect=lambda sid: sessions.get(sid)),
        get_compression_tip=AsyncMock(return_value="tip-1"))
    runner._classify_completion_target = GatewayRunner._classify_completion_target.__get__(runner)
    runner._completion_delivery_ready = GatewayRunner._completion_delivery_ready.__get__(runner)
    claim = await runner._preflight_completion_delivery({
        "type": "async_delegation", "delegation_id": "deleg_waiting", "parent_session_id": "parent-1"})
    assert claim.proceed is False and claim.early_result is False
    assert claim.claim_id == ""
    runner._deliver_platform_notice.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_terminal_notice_is_interim_and_never_seals_an_open_stream(private):
    """A stream-is-the-message adapter seals the first unmarked send; the out-of-band terminal line must be interim."""
    from types import SimpleNamespace
    from gateway.config import Platform

    sends = []

    class StreamAdapter:
        config = SimpleNamespace(extra={"notice_delivery": "private" if private else "public"})

        async def send(self, chat_id, content, metadata=None):
            sends.append(("send", chat_id, content, dict(metadata or {})))
            return SimpleNamespace(success=True, message_id="m1")

        async def send_private_notice(self, chat_id, user_id, content, metadata=None):
            sends.append(("private", chat_id, content, dict(metadata or {})))
            return SimpleNamespace(success=True, message_id="m2")

    runner = object.__new__(GatewayRunner)
    runner.config = None
    runner._delivery_adapter_for = Mock(return_value=StreamAdapter())
    runner._thread_metadata_for_source = Mock(return_value={"thread_id": "t1"})
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1")
    assert await runner._deliver_platform_notice(source, "Background task stopped: x — y.") is True
    (kind, chat_id, content, metadata) = sends[0]
    assert kind == ("private" if private else "send")
    assert metadata.get("_interim_send") is True
    assert metadata.get("thread_id") == "t1"


@pytest.mark.asyncio
async def test_failed_terminal_send_is_retried_later():
    _undeliverable_row("deleg_send_fail")
    (action,) = dr.sweep_retries(now=1_800_000_100.0)
    runner = _gone_runner()
    runner._deliver_platform_notice = AsyncMock(side_effect=RuntimeError("chat offline"))
    await runner._deliver_delegation_retry_action(action)
    (again,) = dr.sweep_retries(now=1_800_000_200.0)
    assert again["kind"] == "terminal"


@pytest.mark.asyncio
async def test_retry_notice_for_closed_parent_settles_terminal_quietly():
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
    assert terminal["kind"] == "terminal" and terminal["text"] == ""
    await runner._deliver_delegation_retry_action(terminal)
    runner._deliver_platform_notice.assert_not_awaited()
    record = dr.retry_status("deleg_closed")
    assert record["retry_state"] == "reported"
    assert record["retry_reason"] == "its conversation ended before the retry could run"


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
    runner._deliver_platform_notice.assert_not_awaited()  # routine billing stop in profile b is quiet
    assert dr.retry_status("deleg_same_id")["retry_state"] == "notified"
    token = set_hermes_home_override(secondary)
    try:
        assert dr.retry_status("deleg_same_id")["retry_state"] == "reported"
    finally:
        reset_hermes_home_override(token)
    assert get_hermes_home() == root


def _accepted_notice(did):
    """A retry notice the parent's adapter accepted, carrying the claim its wake was stamped with."""
    _failed_row(did, "rate_limit")
    dr.schedule_retry(did, now=1_800_000_060.0)
    due = dr.retry_status(did)["retry_due_at"]
    (action,) = dr.sweep_retries(now=due + 1)
    runner = _runner()
    runner._inject_watch_notification = AsyncMock(return_value=True)
    return runner, action, due


@pytest.mark.asyncio
async def test_retry_notice_wake_is_stamped_with_its_ledger_claim():
    runner, action, _ = _accepted_notice("deleg_stamp")
    await runner._deliver_delegation_retry_action(action)
    (_text, evt), _ = runner._inject_watch_notification.await_args
    assert evt["retry_notice_claim"] == action["claim"]
    assert dr.retry_status("deleg_stamp")["retry_state"] == "notified"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["delegate_stop", "session_stop"])
async def test_queued_retry_notice_is_dropped_once_its_retry_is_cancelled(stop):
    """R-RETRY-NOTICE-AFTER-CANCEL: on 2026-10-13 notices were admitted behind a busy parent
    turn, the units were stopped (retry_state=cancelled), and the queued notices still ran."""
    from types import SimpleNamespace
    from gateway.run_turn import GatewayTurnMixin
    from hermes_cli.session_controls import (
        RETRY_WAKE_METADATA_KEY, internal_wake_is_current, is_gateway_wake)

    runner, action, due = _accepted_notice("deleg_cut")
    await runner._deliver_delegation_retry_action(action)
    event = SimpleNamespace(text=action["text"], metadata={
        RETRY_WAKE_METADATA_KEY: {"delegation_id": "deleg_cut", "claim": action["claim"]}})
    assert is_gateway_wake(event)
    assert internal_wake_is_current(event)  # still owed: the queued notice runs

    if stop == "delegate_stop":
        assert dr.cancel_pending_retries(delegation_id="deleg_cut") == {"deleg_cut"}
    else:
        assert dr.cancel_pending_retries(parent_session_id="parent-1") == {"deleg_cut"}
    assert dr.retry_status("deleg_cut")["retry_state"] == "cancelled"
    assert not internal_wake_is_current(event)  # admission, busy-queue and pin fences drop it

    async def _in_executor(func, *args):
        return func(*args)
    turn = SimpleNamespace(_run_in_executor_with_context=_in_executor)
    assert await GatewayTurnMixin._internal_wake_input_at_execution(
        turn, event, "parent-1", event.text) is None  # the model-boundary fence drops it too
    assert dr.sweep_retries(now=due + dr.NOTICE_GRACE_S * 3) == []  # and no reminder follows


def test_retry_notice_wake_is_superseded_by_a_newer_claim():
    from types import SimpleNamespace
    from hermes_cli.session_controls import RETRY_WAKE_METADATA_KEY, internal_wake_is_current

    _failed_row("deleg_old_claim", "rate_limit")
    dr.schedule_retry("deleg_old_claim", now=1_800_000_060.0)
    due = dr.retry_status("deleg_old_claim")["retry_due_at"]
    (first,) = dr.sweep_retries(now=due + 1)
    assert dr.mark_notice_accepted("deleg_old_claim", first["claim"], now=due + 1)
    (reminder,) = dr.sweep_retries(now=due + 1 + dr.NOTICE_GRACE_S + 1)
    stale = SimpleNamespace(text="", metadata={
        RETRY_WAKE_METADATA_KEY: {"delegation_id": "deleg_old_claim", "claim": first["claim"]}})
    current = SimpleNamespace(text="", metadata={
        RETRY_WAKE_METADATA_KEY: {"delegation_id": "deleg_old_claim", "claim": reminder["claim"]}})
    assert not internal_wake_is_current(stale)
    assert internal_wake_is_current(current)
