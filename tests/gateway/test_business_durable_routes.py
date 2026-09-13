"""Business routing must survive final-reply and detached-update handoffs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.platforms.base import SendResult, _thread_metadata_for_event
from gateway.platforms.event import MessageEvent
from gateway.run_startup import GatewayStartupMixin
from gateway.session import SessionSource
from tests.gateway.test_delivery_ledger_producer import _Adapter
from tests.gateway.test_update_command import _make_runner


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["boot", "runtime", "flood"])
async def test_business_final_producer_survives_sqlite_recovery(tmp_path, monkeypatch, recovery):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    adapter = _Adapter()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="private",
                           business_connection_id="business-fixture")
    event = MessageEvent(text="answer me", source=source, message_id="inbound")
    oid = await adapter._record_delivery_obligation(event, "business-route", "final answer", adapter, False)
    assert oid
    dl.mark_failed(oid, "flood_control:1" if recovery == "flood" else "send_path_degraded")
    if recovery in {"boot", "flood"}:
        with dl._connect() as conn:
            conn.execute("UPDATE delivery_obligations SET owner_pid=999999999, owner_started_at=1")
        claimed = dl.sweep_recoverable()
        if recovery == "flood":
            assert len(claimed) == 1 and claimed[0]["adopted"]
            claimed = dl.sweep_failed_for_runtime("telegram", now=dl.time.time() + 3)
    else:
        claimed = dl.sweep_failed_for_runtime("telegram")
    assert len(claimed) == 1
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)
        return SendResult(success=True, message_id="outbound")

    runner = SimpleNamespace(_obligation_adapter=AsyncMock(return_value=SimpleNamespace(send=send)),
                             _consume_delivered_goal_receipt=AsyncMock(),
                             _arm_flood_timers_for_waiting_rows=AsyncMock())
    assert await GatewayStartupMixin._redeliver_claimed_obligations(runner, claimed) == 1
    assert sent[0]["metadata"]["telegram_business_connection_id"] == "business-fixture"


@pytest.mark.parametrize("chat_type", ["private", "dm"])
def test_base_final_send_metadata_keeps_business_route_without_thread(chat_type):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type=chat_type,
                           business_connection_id="business-fixture")
    assert _thread_metadata_for_event(MessageEvent(text="answer me", source=source)) == {
        "telegram_business_connection_id": "business-fixture"}


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_claimed", [False, True], ids=["current-pending", "legacy-claimed"])
async def test_business_update_marker_reconstructs_notification_target(tmp_path, monkeypatch, legacy_claimed):
    import json
    from gateway.update_notifications import read_pending

    runner = _make_runner()
    runner._schedule_update_notification_watch = Mock()
    runner._authorization_adapter = lambda platform, profile: adapter
    runner._session_key_for_source = lambda source: "business-route"
    adapter = SimpleNamespace()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="private",
                           business_connection_id="business-fixture")
    event = MessageEvent(text="/update", source=source, message_id="update-inbound")
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr("gateway.run._resolve_hermes_bin", lambda: ["fixture"])
    monkeypatch.setattr("gateway.slash_commands._spawn_detached_update", Mock())
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    assert await runner._handle_update_command(event) == ""
    marker, data = read_pending(tmp_path)
    claimed = tmp_path / ".update_pending.claimed.json"
    stored = marker
    if legacy_claimed:
        # Compatibility with historical claimed markers, not the current
        # updater's ownership protocol. Current updates retain the pending path.
        stored = marker.rename(claimed)
    fresh_reader = _make_runner()
    fresh_reader._authorization_adapter = lambda platform, profile: adapter
    target = fresh_reader._resolve_update_target(SimpleNamespace(pending=marker, claimed=claimed))
    assert target.metadata["telegram_business_connection_id"] == "business-fixture"
    assert json.loads(stored.read_text())["business_connection_id"] == "business-fixture"


@pytest.mark.parametrize("origin", ["not json", '{"platform":"telegram","chat_id":"other","business_connection_id":"wrong"}',
                                    '{"platform":"slack","chat_id":"42","business_connection_id":"wrong"}'])
def test_lineage_never_borrows_business_connection_from_mismatched_origin(origin):
    from gateway.update_launcher import _route_from_session_lineage
    db = SimpleNamespace(get_session=lambda _: {"source": "telegram", "chat_id": "42", "origin_json": origin})
    _, route = _route_from_session_lineage(db, "session")
    assert "business_connection_id" not in route
