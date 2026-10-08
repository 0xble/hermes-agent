"""Per-chat daily volume ledger and budget (2026-10-05 34507s ban at 0-6 sends/min).

Every long ban since 2026-09-28 ended at a fixed time of day, and both measured windows reached about
1930 in-turn sends before the refusal. The rate budget cannot see a daily cap, so the volume is
counted per chat and per window. Cosmetic traffic is shed first, then non-final notices. Final replies
are never shed.
"""

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import OUTBOUND_NOTICE, OUTBOUND_PROGRESS, SendResult, outbound_class
from plugins.platforms.telegram import daily_quota as dq
from plugins.platforms.telegram.chat_budget import ChatBudgetRateLimiter, ChatOutboundBudget
from plugins.platforms.telegram.daily_quota import DailyQuota

CHAT = "2027045491"


class Wall:
    def __init__(self, t: float):
        self.t = t

    def __call__(self):
        return self.t


def _utc(s: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()


def test_counts_every_endpoint_and_each_created_message(tmp_path):
    wall = Wall(_utc("2026-10-05 12:00:00"))
    quota = DailyQuota(profile_dir=tmp_path, wall=wall)
    quota.record(CHAT, "sendMessage")
    quota.record(CHAT, "editMessageText")
    quota.record(CHAT, "sendChatAction")
    quota.record(CHAT, "deleteMessages", {"message_ids": [1, 2, 3]})
    quota.record(CHAT, "sendMediaGroup", {"media": [{}, {}, {}]})
    assert quota.messages(CHAT) == 4  # one text plus three album items; edits and actions create none
    assert quota.calls(CHAT) == {"sendMessage": 1, "editMessageText": 1, "sendChatAction": 1,
                                 "deleteMessages": 1, "sendMediaGroup": 1}


def test_window_rolls_at_the_anchor_and_starts_empty(tmp_path):
    wall = Wall(_utc("2026-10-05 23:59:00"))
    quota = DailyQuota(profile_dir=tmp_path, wall=wall)
    quota.record(CHAT, "sendMessage")
    wall.t = _utc("2026-10-06 00:00:01")  # default anchor is UTC midnight
    assert quota.messages(CHAT) == 0


def test_counts_survive_a_restart_and_include_the_standalone_lane(tmp_path):
    wall = Wall(_utc("2026-10-05 12:00:00"))
    gateway = DailyQuota(profile_dir=tmp_path, wall=wall)
    for _ in range(5):
        gateway.record(CHAT, "sendMessage")
    gateway.flush()
    standalone = DailyQuota(soft_ceiling=None, profile_dir=tmp_path, wall=wall, flush_interval=0.0)
    standalone.record(CHAT, "sendMessage")
    standalone.record(CHAT, "sendMessage")
    gateway.flush()  # reads the shared total back
    assert gateway.messages(CHAT) == 7
    restarted = DailyQuota(profile_dir=tmp_path, wall=wall)
    assert restarted.messages(CHAT) == 7


def test_long_penalty_anchors_the_window_at_the_reset_and_logs_the_counts(tmp_path, caplog):
    """17:23:52 PDT + 34507s = 09:58:59 UTC, the reset the evidence points at."""
    wall = Wall(_utc("2026-10-06 00:23:52"))
    quota = DailyQuota(profile_dir=tmp_path, wall=wall)
    for _ in range(3):
        quota.record(CHAT, "sendMessage")
    caplog.set_level(logging.WARNING, logger=dq.__name__)
    quota.note_retry_after(CHAT, 34507.0)
    assert any("refused for 34507s after 3 messages" in r.getMessage() for r in caplog.records)
    assert quota.messages(CHAT) == 3  # this traffic belongs to the window that ends at the reset
    wall.t = _utc("2026-10-06 09:58:58")
    assert quota.messages(CHAT) == 3
    wall.t = _utc("2026-10-06 09:59:00")
    assert quota.messages(CHAT) == 0
    restarted = DailyQuota(profile_dir=tmp_path, wall=wall)
    restarted.record(CHAT, "sendMessage")
    wall.t = _utc("2026-10-07 09:58:58")
    assert restarted.messages(CHAT) == 1  # the anchor persisted


def test_short_penalty_does_not_move_the_window(tmp_path):
    wall = Wall(_utc("2026-10-05 12:00:00"))
    quota = DailyQuota(profile_dir=tmp_path, wall=wall)
    quota.record(CHAT, "sendMessage")
    quota.note_retry_after(CHAT, 3.0)
    wall.t = _utc("2026-10-06 00:00:01")
    assert quota.messages(CHAT) == 0  # still the UTC-midnight window


@pytest.mark.parametrize("used, progress, notice", [(0, False, False), (69, False, False), (70, True, False),
                                                    (99, True, False), (100, True, True), (5000, True, True)])
def test_shedding_order_cosmetic_then_notices_never_finals(tmp_path, used, progress, notice):
    quota = DailyQuota(soft_ceiling=100, profile_dir=tmp_path, wall=Wall(_utc("2026-10-05 12:00:00")))
    for _ in range(used):
        quota.record(CHAT, "sendMessage")
    assert quota.sheds(CHAT, OUTBOUND_PROGRESS) is progress
    assert quota.sheds(CHAT, OUTBOUND_NOTICE) is notice
    assert quota.sheds(CHAT, None) is False


def test_typed_turns_keep_progress_under_background_pressure(tmp_path):
    quota = DailyQuota(soft_ceiling=100, profile_dir=tmp_path, wall=Wall(_utc("2026-10-05 12:00:00")))
    for _ in range(70):
        quota.record(CHAT, "sendMessage")
    assert quota.sheds(CHAT, OUTBOUND_PROGRESS, "delegation") is True
    assert quota.sheds(CHAT, OUTBOUND_PROGRESS, "typed") is False


def test_disabled_ceiling_counts_but_never_sheds(tmp_path):
    quota = DailyQuota(soft_ceiling=0, profile_dir=tmp_path, wall=Wall(_utc("2026-10-05 12:00:00")))
    for _ in range(5000):
        quota.record(CHAT, "sendMessage")
    assert quota.messages(CHAT) == 5000 and not quota.sheds(CHAT, OUTBOUND_PROGRESS)


@pytest.mark.asyncio
async def test_limiter_records_every_wire_call_and_sheds_cosmetics_under_pressure(tmp_path):
    from telegram.error import RetryAfter

    quota = DailyQuota(soft_ceiling=10, profile_dir=tmp_path)
    limiter = ChatBudgetRateLimiter(ChatOutboundBudget(gap_override=0), daily=quota)
    wire = AsyncMock(return_value=True)
    for _ in range(7):
        await limiter.process_request(wire, (), {}, "sendMessage", {"chat_id": CHAT}, None)
    assert quota.messages(CHAT) == 7
    await limiter.process_request(wire, (), {}, "sendChatAction", {"chat_id": CHAT}, None)
    await limiter.process_request(wire, (), {}, "sendMessageDraft", {"chat_id": CHAT}, None)
    assert wire.await_count == 7  # typing and drafts shed at 70% of the ceiling
    await limiter.process_request(wire, (), {}, "getChat", {"chat_id": CHAT}, None)
    assert "getChat" not in quota.calls(CHAT)

    async def banned():
        raise RetryAfter(dt.timedelta(seconds=34507))

    with pytest.raises(RetryAfter):
        await limiter.process_request(banned, (), {}, "editMessageText", {"chat_id": CHAT}, None)
    assert quota.calls(CHAT)["editMessageText"] == 1


def _adapter(tmp_path, ceiling=10):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***",
                                             extra={"daily_message_soft_ceiling": ceiling}))
    adapter._update_receipt_dir = tmp_path
    adapter._rich_messages_enabled = False
    adapter._rich_send_disabled = True
    adapter._telegram_chat_outbound_slot_secs = 0.0
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    adapter._bot.send_chat_action = AsyncMock(return_value=True)
    adapter._bot.edit_message_text = AsyncMock(return_value=MagicMock())
    adapter._bot.delete_messages = AsyncMock(return_value=True)
    adapter._bot.delete_message = AsyncMock(return_value=True)
    return adapter


def _spend(adapter, n):
    for _ in range(n):
        adapter._daily_quota().record(CHAT, "sendMessage")


def test_ceiling_comes_from_platform_extra(tmp_path):
    assert _adapter(tmp_path, ceiling=1234)._daily_quota().soft_ceiling == 1234


@pytest.mark.asyncio
async def test_installed_request_limiter_shares_the_adapter_quota(tmp_path):
    """The PTB limiter built for the live bot must meter and shed with the adapter's own ledger."""
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    limiter = adapter._chat_rate_limiter()
    assert limiter.daily is adapter._daily_quota()
    wire = AsyncMock(return_value=True)
    for _ in range(7):
        await limiter.process_request(wire, (), {}, "sendMessage", {"chat_id": CHAT}, None)
    assert adapter._daily_quota().messages(CHAT) == 7
    token = bind_trigger(SimpleNamespace(text="[ASYNC DELEGATION BATCH COMPLETE]", internal=True))
    try:
        await limiter.process_request(wire, (), {}, "sendChatAction", {"chat_id": CHAT}, None)
    finally:
        reset_trigger(token)
    assert wire.await_count == 7  # background typing shed through the installed limiter


@pytest.mark.asyncio
async def test_cosmetic_pressure_sheds_typing_interim_edits_progress_and_cleanup(tmp_path):
    adapter = _adapter(tmp_path)
    _spend(adapter, 7)
    await adapter.send_typing(CHAT)
    adapter._bot.send_chat_action.assert_not_awaited()
    skipped = await adapter.edit_message(CHAT, "5", "interim", finalize=False)
    assert skipped.raw_response == {"skipped": True}
    adapter._bot.edit_message_text.assert_not_awaited()
    assert await adapter.delete_messages(CHAT, ["1", "2"]) == {"1": False, "2": False}
    assert await adapter.delete_message(CHAT, "3") is False
    adapter._bot.delete_messages.assert_not_awaited()
    with outbound_class(OUTBOUND_PROGRESS):
        shed = await adapter.send(CHAT, "⚙️ progress")
    assert shed.error == "daily_budget_shed"
    with outbound_class(OUTBOUND_NOTICE):
        assert (await adapter.send(CHAT, "status")).success  # notices still go below the ceiling
    assert (await adapter.edit_message(CHAT, "5", "final answer", finalize=True)).success


@pytest.mark.asyncio
async def test_ceiling_sheds_notices_but_never_a_final(tmp_path):
    adapter = _adapter(tmp_path)
    _spend(adapter, 10)
    with outbound_class(OUTBOUND_NOTICE):
        assert (await adapter.send(CHAT, "status")).error == "daily_budget_shed"
    assert (await adapter.send(CHAT, "advisory", metadata={"_interim_send": True})).error == "daily_budget_shed"
    sent_before = adapter._bot.send_message.await_count
    final = await adapter.send(CHAT, "the answer")
    assert final.success and adapter._bot.send_message.await_count == sent_before + 1


@pytest.mark.asyncio
async def test_busy_reply_label_survives_the_spawned_task(tmp_path):
    from gateway.platforms.base import ingress_consumer_scope, leave_ingress_consumer
    from gateway.run import GatewayRunner

    adapter = _adapter(tmp_path)
    _spend(adapter, 10)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = lambda event: None
    runner._busy_reply_to = lambda event, anchor: None
    runner._thread_metadata_for_source = lambda source, anchor: {}
    event = SimpleNamespace(source=SimpleNamespace(chat_id=CHAT, thread_id=None), metadata={}, message_id="1")
    token = ingress_consumer_scope()
    try:
        await runner._send_busy_reply(event, adapter, "Queued for the next turn.")
    finally:
        leave_ingress_consumer(token)
    await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_shed_notice_is_final_no_retry_fallback_or_failure_notice(tmp_path):
    adapter = _adapter(tmp_path)
    _spend(adapter, 10)
    calls = []
    original = adapter.send

    async def counting_send(*args, **kwargs):
        calls.append(kwargs.get("content", args[1] if len(args) > 1 else None))
        return await original(*args, **kwargs)

    adapter.send = counting_send
    with outbound_class(OUTBOUND_NOTICE):
        result = await adapter._send_with_retry(CHAT, "status line")
    assert result.error == "daily_budget_shed"
    assert calls == ["status line"]  # no retry, plain-text copy, or "delivery failed" notice
    adapter._bot.send_message.assert_not_awaited()
