"""Display-only terminal TTL tests for delegation cards."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import delegation_card_presentation as presentation
from gateway.delegation_cards import DelegationCards
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource


TERMINAL = ("completed", "failed", "error", "cancelled", "interrupted", "timeout", "budget_exhausted")


def _row(state, *, parent=None, terminal_at=None, expires=None):
    row = {"thread_ref": state, "task_label": state, "state": state}
    if parent is not None:
        row["card_parent_identity"] = parent
    if terminal_at is not None:
        row["terminal_at"] = terminal_at
    if expires is not None:
        row["display_expires_at"] = expires
    return row


async def _drain(manager):
    for _ in range(20):
        pending = list(manager.pending.values())
        if not pending:
            return
        await asyncio.gather(*pending)
    pytest.fail("delegation card presentation did not settle")


def _card(key, *, now, expires, rows, message_id):
    owner = {"profile": "default", "session_id": "s", "session_key": "route",
             "chat_id": "42", "thread_id": "8"}
    return {"owner": owner.copy(), "delegation_owner": owner.copy(),
            "source": {"platform": "telegram", "chat_id": "42", "thread_id": "8"},
            "started_at": now - 10, "generation": 0, "revision": 0, "rows": rows,
            "message_id": message_id, "rendered": "", "recoveries": 0,
            "send_attempts": 1, "retired": False, "handled": []}


@pytest.mark.parametrize("state", TERMINAL)
def test_terminal_rows_expire_but_running_and_queued_rows_do_not(state):
    projection = {"rows": {
        "expired": _row(state, terminal_at=10, expires=20),
        "running": _row("running"),
        "queued": _row("queued"),
    }}
    selected = presentation.select_rows(projection, now=20, max_visible_roots=5)
    assert selected == {"running", "queued"}


def test_expired_ancestor_is_retained_as_context_for_live_descendant():
    projection = {"rows": {
        "root": _row("completed", terminal_at=10, expires=20),
        "child": _row("running", parent="root"),
    }}
    assert presentation.select_rows(projection, now=20) == {"root", "child"}


def test_cap_is_applied_before_ttl_without_backfilling_old_roots():
    projection = {"rows": {
        "old": _row("running"),
        "new-expired": _row("completed", terminal_at=10, expires=20),
        "new-live": _row("running"),
    }}
    assert presentation.select_rows(projection, now=20, max_visible_roots=2) == {"new-live"}


def test_legacy_terminal_without_timestamp_is_immediately_hidden():
    projection = {"rows": {"legacy": _row("completed")}}
    assert presentation.select_rows(projection, now=0) == set()
    assert "terminal_at" not in projection["rows"]["legacy"]


def test_invalid_ttl_config_falls_back_and_null_platform_override_inherits():
    from gateway.display_config import resolve_display_setting
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["display"]["delegation_terminal_ttl_seconds"] == 300
    assert resolve_display_setting({"display": {"delegation_terminal_ttl_seconds": 0}}, "telegram",
                                   "delegation_terminal_ttl_seconds", 300) == 300
    assert resolve_display_setting({"display": {"delegation_terminal_ttl_seconds": 17,
                                                   "platforms": {"telegram": {"delegation_terminal_ttl_seconds": None}}}},
                                   "telegram", "delegation_terminal_ttl_seconds", 300) == 17
    assert resolve_display_setting({"display": {"platforms": {"telegram": {"delegation_terminal_ttl_seconds": True}}}},
                                   "telegram", "delegation_terminal_ttl_seconds", 300) == 300


@pytest.mark.asyncio
async def test_first_terminal_timestamp_is_persisted_and_duplicate_does_not_reset(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(presentation, "terminal_ttl_seconds", lambda manager, key: 30)
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")))
    runner = SimpleNamespace(_adapter_for_source=lambda source: adapter)
    cards = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda: now[0])
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    key = "a" * 32
    data = dict(parent_task_id=key, thread_ref="A", task_label="TTL", owner=dict(
        profile="default", session_id="s", session_key="route", chat_id="42", thread_id=""))

    await cards.observe(source, "route", "s", 1, "subagent.start", None, data)
    await cards.observe(source, "route", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    row = cards.cards[key]["rows"]["A"]
    assert row["terminal_at"] == 100.0
    assert row["display_expires_at"] == 130.0
    assert len(cards._expiry_timers) == 1
    now[0] = 120.0
    await cards.observe(source, "route", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    assert row["terminal_at"] == 100.0
    assert row["display_expires_at"] == 130.0
    assert row["state"] == "completed"
    await cards.shutdown()


def test_restart_derives_missing_deadline_but_does_not_fabricate_missing_terminal_time(tmp_path, monkeypatch):
    monkeypatch.setattr(presentation, "terminal_ttl_seconds", lambda manager, key: 30)
    key = "b" * 32
    cards_path = tmp_path / "cache" / "delegation" / "cards.json"
    cards_path.parent.mkdir(parents=True)
    import json
    cards_path.write_text(json.dumps({key: {
        "owner": {"profile": "default", "session_id": "s", "session_key": "r", "chat_id": "42", "thread_id": ""},
        "source": {"platform": "telegram", "chat_id": "42"}, "started_at": 1, "generation": 0, "revision": 0,
        "retired": False, "handled": [],
        "rows": {"valid": _row("completed", terminal_at=100), "legacy": _row("completed")},
    }}))
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda source: None), home=tmp_path, clock=lambda: 200)
    assert cards.cards[key]["rows"]["valid"]["display_expires_at"] == 130
    assert "display_expires_at" not in cards.cards[key]["rows"]["legacy"]
    assert presentation.select_rows(cards._projection(key), now=200) == set()


@pytest.mark.asyncio
async def test_duplicate_completion_restart_and_resume_at_expiry_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(presentation, "terminal_ttl_seconds", lambda manager, key: 30)
    now = [100.0]
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(side_effect=[
            SendResult(success=True, message_id="1"), SendResult(success=True, message_id="2")]),
        edit_message=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        delete_message=AsyncMock(return_value=True),
    )
    runner = SimpleNamespace(_adapter_for_source=lambda source: adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    key = "c" * 32
    owner = dict(profile="default", session_id="s", session_key="route", chat_id="42", thread_id="8")
    data = dict(parent_task_id=key, thread_ref="A", task_label="Boundary", owner=owner)

    manager = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda: now[0])
    await manager.observe(source, "route", "s", 1, "subagent.start", None, data)
    await _drain(manager)
    await manager.observe(source, "route", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    await _drain(manager)
    row = manager.cards[key]["rows"]["A"]
    assert (row["terminal_at"], row["display_expires_at"]) == (100.0, 130.0)

    now[0] = 129.0
    await manager.observe(source, "route", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    assert (row["terminal_at"], row["display_expires_at"]) == (100.0, 130.0)
    await manager.shutdown()

    # Restart exactly on the deadline: the scheduler/reconcile path must remove
    # only the display message, not the durable row needed for a continuation.
    now[0] = 130.0
    restored = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda: now[0])
    await restored.reconcile()
    await _drain(restored)
    assert adapter.delete_message.await_count == 1
    assert restored.cards[key]["rows"]["A"]["state"] == "completed"
    assert restored.cards[key]["message_id"] is None

    await restored.observe(source, "route", "s", 1, "subagent.admitted", None, {
        **data, "attempt": 1, "resume_claim_id": "resume-1"})
    await _drain(restored)
    assert restored.cards[key]["rows"]["A"]["state"] == "running"
    assert adapter.send_delegation_card.await_count == 2


@pytest.mark.asyncio
async def test_one_timer_per_shared_scope_rejects_stale_callback_and_renders_new_anchor(tmp_path):
    now = [100.0]
    adapter = SimpleNamespace(
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True),
        send_delegation_card=AsyncMock(),
    )
    manager = DelegationCards(SimpleNamespace(_adapter_for_source=lambda source: adapter),
                              home=tmp_path, interval=0, clock=lambda: now[0])
    rows = lambda ref, label, expires: {ref: _row("completed", terminal_at=90, expires=expires) | {
        "task_label": label}}
    manager.cards["a" * 32] = _card("a" * 32, now=100, expires=110,
                                    rows=rows("A", "Old anchor", 110), message_id="a-msg")
    manager.cards["b" * 32] = _card("b" * 32, now=100, expires=120,
                                    rows=rows("B", "New anchor", 120), message_id="b-msg")
    # Make B the stable survivor when the lifecycle flush binds this shared
    # scope; the callback must still target the newly scheduled anchor.
    manager.cards["a" * 32]["started_at"] = 101
    manager.cards["b" * 32]["started_at"] = 90
    manager._schedule_expiry("a" * 32)
    scope = manager._scope(manager.cards["a" * 32])
    stale_token = manager._expiry_tokens[scope]
    manager._schedule_expiry("b" * 32)
    current_token = manager._expiry_tokens[scope]
    assert stale_token != current_token

    manager._expiry_callback(scope, "a" * 32, stale_token)
    assert not manager.pending
    manager._expiry_callback(scope, "b" * 32, current_token)
    assert set(manager.pending) == {"b" * 32}
    await _drain(manager)
    assert adapter.edit_message.await_count == 1
    assert adapter.edit_message.await_args.args[1] == "b-msg"


@pytest.mark.asyncio
async def test_expired_sibling_is_pruned_but_active_descendant_keeps_ancestor_context(tmp_path):
    now = [109.0]
    adapter = SimpleNamespace(
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True), send_delegation_card=AsyncMock())
    manager = DelegationCards(SimpleNamespace(_adapter_for_source=lambda source: adapter),
                              home=tmp_path, interval=0, clock=lambda: now[0])
    key = "d" * 32
    manager.cards[key] = _card(key, now=100, expires=110, message_id="msg", rows={
        "root": {**_row("completed", terminal_at=90, expires=110), "task_label": "Root"},
        "sibling": {**_row("completed", terminal_at=90, expires=110), "task_label": "Expired sibling"},
        "child": {**_row("running", parent="root"), "task_label": "Active child",
                   "card_parent_task_id": key, "card_parent_thread_ref": "root"},
    })
    manager._schedule_expiry(key)
    scope = manager._scope(manager.cards[key])
    token = manager._expiry_tokens[scope]
    now[0] = 110.0
    manager._expiry_callback(scope, key, token)
    await _drain(manager)
    assert not adapter.delete_message.await_args_list
    text = adapter.edit_message.await_args.args[2]
    assert "Root" in text and "Active child" in text
    assert "Expired sibling" not in text


@pytest.mark.asyncio
async def test_empty_delete_is_idempotent_then_refill_reuses_tombstone(tmp_path, monkeypatch):
    monkeypatch.setattr(presentation, "terminal_ttl_seconds", lambda manager, key: 10)
    now = [100.0]
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="refilled")),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True),
    )
    runner = SimpleNamespace(_adapter_for_source=lambda source: adapter)
    manager = DelegationCards(runner, home=tmp_path, interval=0, clock=lambda: now[0])
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    key = "e" * 32
    owner = dict(profile="default", session_id="s", session_key="route", chat_id="42", thread_id="8")
    data = dict(parent_task_id=key, thread_ref="A", task_label="Refill", owner=owner)
    await manager.observe(source, "route", "s", 1, "subagent.start", None, data)
    await _drain(manager)
    await manager.observe(source, "route", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    await _drain(manager)
    now[0] = 110.0
    scope = manager._scope(manager.cards[key])
    token = manager._expiry_tokens[scope]
    manager._expiry_callback(scope, key, token)
    await _drain(manager)
    assert adapter.delete_message.await_count == 1

    # A second empty pass has no message to delete and must not spend a retry.
    manager._queue(key)
    await _drain(manager)
    assert adapter.delete_message.await_count == 1

    await manager.observe(source, "route", "s", 1, "subagent.admitted", None, {
        **data, "attempt": 1, "resume_claim_id": "refill-1"})
    await _drain(manager)
    assert manager.cards[key]["rows"]["A"]["state"] == "running"
    assert manager.cards[key]["message_id"] == "refilled"

@pytest.mark.asyncio
async def test_expired_consolidated_cards_delete_exact_receipts_without_retirement(tmp_path):
    adapter = SimpleNamespace(delete_message=AsyncMock(return_value=True))
    manager = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter),
                              home=tmp_path, interval=0, clock=lambda: 200)
    for key, message in [("a" * 32, "first"), ("b" * 32, "second")]:
        manager.cards[key] = _card(key, now=100, expires=110, message_id=message,
            rows={"A": _row("completed", terminal_at=100, expires=110)})
    await manager.reconcile()
    await _drain(manager)
    assert {call.args[1] for call in adapter.delete_message.await_args_list} == {"first", "second"}
    assert all(not card.get("retired") and not card.get("handled") for card in manager.cards.values())
    entries = [entry for card in manager.cards.values() for entry in card.get("presentation_cleanup", [])]
    assert entries and all(entry["state"] == "deleted" and entry.get("projection_expired")
                           and not entry.get("projection_retired") for entry in entries)
    await manager.shutdown()
