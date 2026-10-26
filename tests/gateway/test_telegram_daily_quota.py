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


def test_failed_load_is_retried_and_keeps_counts_made_meanwhile(tmp_path, monkeypatch):
    wall = Wall(_utc("2026-10-05 12:00:00"))
    writer = DailyQuota(profile_dir=tmp_path, wall=wall)
    for _ in range(5):
        writer.record(CHAT, "sendMessage")
    writer.flush()

    reader = DailyQuota(profile_dir=tmp_path, wall=wall)
    real_db = reader._db
    monkeypatch.setattr(reader, "_db", lambda: (_ for _ in ()).throw(dq.sqlite3.OperationalError("database is locked")))
    reader.record(CHAT, "sendMessage")  # store locked: counted in memory only
    monkeypatch.setattr(reader, "_db", real_db)
    assert reader.messages(CHAT) == 6  # the retried load sees the persisted 5 plus the in-memory 1
    reader.flush()
    assert DailyQuota(profile_dir=tmp_path, wall=wall).messages(CHAT) == 6


@pytest.mark.asyncio
async def test_goal_notice_retry_stays_labelled_as_a_notice():
    from gateway.platforms.base import current_outbound_class
    from gateway.run import GatewayRunner

    seen = []

    class Adapter:
        async def send(self, chat_id, message, metadata=None):
            seen.append(current_outbound_class())
            return SendResult(success=True)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._goal_notice_adapter = lambda source: Adapter()
    await runner._retry_goal_status_notice(SimpleNamespace(chat_id=CHAT), "↻ continuing", None, 0, 0, attempts=1)
    assert seen == [OUTBOUND_NOTICE]


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


def _voice_runner_and_event(adapter):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(stt_enabled=True)
    runner._queued_events = {}
    runner._draining = False
    runner._session_run_generation = {}
    runner._should_echo_stt_transcripts = lambda: True
    runner._delivery_adapter_for = lambda source: adapter
    runner._reply_anchor_for_event = lambda event: None
    runner._thread_metadata_for_source = lambda source, anchor: {"thread_id": source.thread_id}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT, thread_id="42", chat_type="group")
    event = MessageEvent(
        text="", message_type=MessageType.VOICE, source=source,
        media_urls=["/voice.ogg"], media_types=["audio/ogg"],
    )
    runner._enrich_message_with_transcription = AsyncMock(return_value=('"queued words"', ["queued words"]))
    return runner, event


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["prefetch", "overflow-drain", "idle"])
@pytest.mark.parametrize("trigger", ["goal", "untagged"])
async def test_busy_voice_echo_survives_background_daily_pressure(tmp_path, path, trigger):
    """Use the real gateway queue/drain and Telegram send, faking only STT and the Bot API."""
    from plugins.platforms.telegram.chat_budget import bind_trigger, current_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    _spend(adapter, 11)  # already above the soft ceiling
    runner, event = _voice_runner_and_event(adapter)
    session_key = "telegram:group:voice-topic"
    token = (
        bind_trigger(SimpleNamespace(text="[Continuing toward your standing goal]", internal=True))
        if trigger == "goal" else None
    )
    try:
        assert current_trigger() == trigger
        with outbound_class(OUTBOUND_NOTICE):  # inherit the busy background turn's outbound context too
            if path == "prefetch":
                adapter.send = AsyncMock(wraps=adapter.send)
                runner._enqueue_fifo(session_key, event, adapter)
                await event._gateway_pending_stt_prefetch
                assert adapter._pending_messages[session_key] is event
                assert adapter.send.await_args.kwargs["metadata"]["_interim_send"] is True
            elif path == "idle":
                assert await runner._enrich_inbound_voice(
                    event, event.source, "", event.media_urls,
                ) == '"queued words"'
            else:
                # Overflow promotion bypasses Telegram.get_pending_message's trigger rebinding.
                runner._session_state(session_key).conversation.queued_events.append(event)
                drained, text = await runner._run_agent_drain_pending(
                    {"final_response": "done"}, adapter, event.source, session_key,
                )
                assert drained is event and text == '"queued words"'
        adapter._bot.send_message.assert_awaited_once()
        sent = adapter._bot.send_message.await_args.kwargs
        assert "🎙️" in sent["text"] and "queued words" in sent["text"]
        assert sent["message_thread_id"] == 42
        runner._enrich_message_with_transcription.assert_awaited_once()
        assert current_trigger() == trigger  # no echo exemption leaks into background sends
        with outbound_class(OUTBOUND_NOTICE):
            assert (await adapter.send(CHAT, "goal notice")).error == "daily_budget_shed"
        with outbound_class(OUTBOUND_PROGRESS):
            assert (await adapter.send(CHAT, "background progress")).error == "daily_budget_shed"
        adapter._bot.send_message.assert_awaited_once()
    finally:
        if token is not None:
            reset_trigger(token)


@pytest.mark.asyncio
async def test_internal_voice_echo_does_not_gain_user_priority(tmp_path, caplog):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    _spend(adapter, 11)
    runner, event = _voice_runner_and_event(adapter)
    event.internal = True
    token = bind_trigger(SimpleNamespace(text="[Continuing toward your standing goal]", internal=True))
    try:
        with outbound_class(OUTBOUND_NOTICE):
            await runner._echo_pending_stt_transcripts_once(
                event, adapter, event.source, ["internal words"], metadata={"_interim_send": True},
            )
    finally:
        reset_trigger(token)
    adapter._bot.send_message.assert_not_awaited()
    assert any("daily_budget_shed" in r.getMessage() and r.levelno >= logging.INFO for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["daily_budget_shed", "raised"])
async def test_failed_voice_echo_is_visible_without_debug_logging(tmp_path, caplog, failure):
    adapter = _adapter(tmp_path)
    runner, event = _voice_runner_and_event(adapter)
    adapter.send = AsyncMock(
        side_effect=RuntimeError("echo transport failed") if failure == "raised" else None,
        return_value=SendResult(success=False, error="daily_budget_shed"),
    )
    caplog.set_level(logging.INFO, logger="gateway.run")
    await runner._echo_pending_stt_transcripts_once(
        event, adapter, event.source, ["private transcript"], log_context="Voice-queue",
    )
    assert any(
        r.levelno >= logging.INFO and "Voice-queue echo failed" in r.getMessage()
        and ("daily_budget_shed" if failure == "daily_budget_shed" else "echo transport failed") in r.getMessage()
        for r in caplog.records
    )
    assert all("private transcript" not in r.getMessage() for r in caplog.records)


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
async def test_cosmetic_pressure_sheds_typing_interim_edits_and_progress(tmp_path):
    adapter = _adapter(tmp_path)
    _spend(adapter, 7)
    await adapter.send_typing(CHAT)
    adapter._bot.send_chat_action.assert_not_awaited()
    skipped = await adapter.edit_message(CHAT, "5", "interim", finalize=False)
    assert skipped.raw_response == {"skipped": True}
    adapter._bot.edit_message_text.assert_not_awaited()
    with outbound_class(OUTBOUND_PROGRESS):
        shed = await adapter.send(CHAT, "⚙️ progress")
    assert shed.error == "daily_budget_shed"
    # Mid-turn commentary is progress too: shed at the cosmetic threshold, before the notice ceiling.
    assert (await adapter.send(CHAT, "commentary", metadata={"_interim_send": True})).error == "daily_budget_shed"
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


@pytest.mark.asyncio
async def test_background_pressure_never_sheds_cleanup_deletes(tmp_path):
    """Deletes create no messages, so shedding them saves none of the daily cap and strands bubbles.

    Regression: a typed turn's end-of-turn cleanup ran labelled with a drained background follow-up's
    trigger, was shed, and left every progress bubble behind (``returned_false``).
    """
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    _spend(adapter, 7)  # past the cosmetic threshold
    token = bind_trigger(SimpleNamespace(text="[IMPORTANT: Background process 42 finished]", internal=True))
    try:
        assert adapter._daily_sheds(CHAT, OUTBOUND_PROGRESS)  # background progress is still shed
        assert await adapter.delete_messages(CHAT, ["1", "2"]) == {"1": True, "2": True}
        assert await adapter.delete_message(CHAT, "3") is True
    finally:
        reset_trigger(token)
    adapter._bot.delete_messages.assert_awaited_once()
    adapter._bot.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_delivery_cleanup_keeps_the_registering_turns_trigger(tmp_path):
    """Draining a queued background event relabels the task; the prior turn's cleanup must not inherit it."""
    from plugins.platforms.telegram.chat_budget import bind_trigger, current_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    seen = []
    token = bind_trigger(SimpleNamespace(text="please check the build"))
    try:
        adapter.register_post_delivery_callback("sk", lambda: seen.append(current_trigger()), generation=1)
        adapter._pending_messages["sk"] = SimpleNamespace(
            text="[IMPORTANT: Background process 42 finished]", internal=True)
        adapter.get_pending_message("sk")  # the runner's in-band drain
        assert current_trigger() == "process"
        callback = adapter.pop_post_delivery_callback("sk", generation=1)
        result = callback()
        if asyncio.iscoroutine(result):
            await result
        assert seen == ["typed"]
        assert current_trigger() == "process"  # the follow-up turn keeps its own label afterwards
    finally:
        reset_trigger(token)


# --- Daily call budget (2026-10-24: 36261s ban at 19875 calls, 10116 of them from typed turns) ---


def _spend_calls(quota, n, endpoint="editMessageText"):
    for _ in range(n):
        quota.record(CHAT, endpoint)


@pytest.mark.parametrize("calls, background, typed", [(0, False, False), (49, False, False), (50, True, False),
                                                      (99, True, False), (100, True, True)])
def test_call_budget_sheds_background_at_half_and_typed_cosmetics_at_the_ceiling(tmp_path, calls, background,
                                                                                 typed):
    quota = DailyQuota(soft_ceiling=1500, call_soft_ceiling=100, profile_dir=tmp_path,
                       wall=Wall(_utc("2026-10-24 12:00:00")))
    _spend_calls(quota, calls)  # edits and typing create no messages, so the message rule never engages
    assert quota.messages(CHAT) == 0
    assert quota.sheds(CHAT, OUTBOUND_PROGRESS, "goal") is background
    assert quota.sheds(CHAT, OUTBOUND_PROGRESS, "typed") is typed
    assert quota.sheds(CHAT, OUTBOUND_NOTICE, "goal") is typed  # background notices go at the ceiling
    assert quota.sheds(CHAT, OUTBOUND_NOTICE, "typed") is False
    assert quota.sheds(CHAT, None, "typed") is False and quota.sheds(CHAT, None, "goal") is False


def test_long_penalty_halves_the_call_budget_for_a_day_across_restarts(tmp_path):
    """The 17:03 PDT ban came 15h after the 02:17 PDT one, at fewer calls."""
    wall = Wall(_utc("2026-10-24 09:17:52"))
    quota = DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path, wall=wall)
    assert quota.call_ceiling(CHAT) == 100
    quota.note_retry_after(CHAT, 2967.0)
    assert quota.call_ceiling(CHAT) == 50
    wall.t = _utc("2026-10-24 12:00:00")  # a new window, still within a day of the penalty
    restarted = DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path, wall=wall)
    _spend_calls(restarted, 25)
    assert restarted.call_ceiling(CHAT) == 50
    assert restarted.sheds(CHAT, OUTBOUND_PROGRESS, "goal") is True
    _spend_calls(restarted, 25)
    assert restarted.sheds(CHAT, OUTBOUND_PROGRESS, "typed") is True
    wall.t = _utc("2026-10-25 10:07:18")  # the strict day runs 24h from the ban's end (10:07:19)
    assert DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path, wall=wall).call_ceiling(CHAT) == 50
    wall.t = _utc("2026-10-25 10:07:20")
    assert DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path, wall=wall).call_ceiling(CHAT) == 100


def test_burst_penalty_keeps_the_full_call_budget(tmp_path):
    quota = DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path, wall=Wall(_utc("2026-10-24 01:10:00")))
    quota.note_retry_after(CHAT, 5.0)  # the daily 18:10 PDT sendChatAction refusal
    assert quota.call_ceiling(CHAT) == 100


def test_disabled_call_budget_counts_but_never_sheds_typed(tmp_path):
    quota = DailyQuota(call_soft_ceiling=0, profile_dir=tmp_path, wall=Wall(_utc("2026-10-24 12:00:00")))
    _spend_calls(quota, 50000)
    assert quota.call_ceiling(CHAT) is None
    assert quota.sheds(CHAT, OUTBOUND_PROGRESS, "typed") is False
    assert sum(quota.calls(CHAT).values()) == 50000


@pytest.mark.asyncio
async def test_limiter_sheds_typed_typing_once_the_call_budget_is_spent(tmp_path):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    limiter = ChatBudgetRateLimiter(ChatOutboundBudget(gap_override=0), daily=quota)
    wire = AsyncMock(return_value=True)
    token = bind_trigger(SimpleNamespace(text="what changed?"))
    try:
        for _ in range(10):
            await limiter.process_request(wire, (), {}, "editMessageText", {"chat_id": CHAT}, None)
        await limiter.process_request(wire, (), {}, "sendChatAction", {"chat_id": CHAT}, None)
        assert wire.await_count == 10  # typing shed for a typed turn at the ceiling
        await limiter.process_request(wire, (), {}, "sendMessage", {"chat_id": CHAT}, None)
        assert wire.await_count == 11  # the reply still goes out
    finally:
        reset_trigger(token)


def test_call_ceiling_comes_from_platform_extra(tmp_path):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***", extra={"daily_call_soft_ceiling": 9000}))
    adapter._update_receipt_dir = tmp_path
    assert adapter._daily_quota().call_soft_ceiling == 9000
    assert _adapter(tmp_path / "default")._daily_quota().call_soft_ceiling == dq.DEFAULT_CALL_SOFT_CEILING


def test_untagged_traffic_is_outside_the_call_budget(tmp_path):
    """Busy acks, button taps and restart notes run untagged and answer the user."""
    quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path, wall=Wall(_utc("2026-10-24 12:00:00")))
    _spend_calls(quota, 10)
    assert quota.sheds(CHAT, OUTBOUND_NOTICE, "untagged") is False
    assert quota.sheds(CHAT, OUTBOUND_NOTICE, "goal") is True
    # Restart and recovery notes ride untagged _interim_send: the call rule spares them too.
    assert quota.sheds(CHAT, OUTBOUND_PROGRESS, "untagged") is False
    assert quota.sheds(CHAT, OUTBOUND_PROGRESS, "goal") is True


def test_standalone_lane_penalty_reaches_the_running_gateway(tmp_path):
    wall = Wall(_utc("2026-10-24 09:17:52"))
    gateway = DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path, wall=wall)
    assert gateway.call_ceiling(CHAT) == 100
    lane = DailyQuota(soft_ceiling=None, call_soft_ceiling=None, profile_dir=tmp_path, wall=wall, flush_interval=0.0)
    lane.note_retry_after(CHAT, 2967.0)
    gateway.flush()
    assert gateway.call_ceiling(CHAT) == 50


@pytest.mark.parametrize("trigger, expected", [("typed", None), ("goal", OUTBOUND_PROGRESS), ("untagged", OUTBOUND_PROGRESS)])
def test_typed_interim_sends_are_not_inferred_as_progress(trigger, expected):
    """/login codes, approval fallbacks and stuck warnings ride ``_interim_send`` without a label."""
    assert dq.send_outbound_class(None, {"_interim_send": True}, trigger) == expected
    assert dq.send_outbound_class(OUTBOUND_PROGRESS, {"_interim_send": True}, "typed") == OUTBOUND_PROGRESS
    assert dq.send_outbound_class(None, {}, "goal") is None


@pytest.mark.asyncio
async def test_typed_interim_notice_is_delivered_at_the_call_ceiling(tmp_path):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(adapter._daily_quota(), 10)
    token = bind_trigger(SimpleNamespace(text="/login"))
    try:
        result = await adapter.send(CHAT, "Your device code is ABCD-1234", metadata={"_interim_send": True})
    finally:
        reset_trigger(token)
    assert result.error != "daily_budget_shed"
    adapter._bot.send_message.assert_awaited()


def test_shorter_penalty_inside_a_longer_one_keeps_the_later_end(tmp_path):
    wall = Wall(_utc("2026-10-25 00:03:00"))
    quota = DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path, wall=wall)
    quota.note_retry_after(CHAT, 36261.0)  # ends 10:07:21
    quota.note_retry_after(CHAT, 700.0)
    wall.t = _utc("2026-10-26 10:07:00")  # within 24h of the long ban's end
    assert quota.call_ceiling(CHAT) == 50
    assert DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path, wall=wall).call_ceiling(CHAT) == 50


@pytest.mark.asyncio
async def test_typed_turn_at_the_call_ceiling_drops_typing_but_keeps_its_final_edit(tmp_path):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(adapter._daily_quota(), 10)
    token = bind_trigger(SimpleNamespace(text="what changed?"))
    try:
        await adapter.send_typing(CHAT)
        adapter._bot.send_chat_action.assert_not_awaited()
        assert (await adapter.edit_message(CHAT, "5", "the answer", finalize=True)).success
        adapter._bot.edit_message_text.assert_awaited()
    finally:
        reset_trigger(token)


def _progress_runner(adapter, grouping="edit"):
    import queue
    from gateway.run_turn_runner import TurnRunner

    runner = TurnRunner.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        source=SimpleNamespace(chat_id=CHAT), _progress_reply_to=None,
        _progress_metadata={"thread_id": "42"}, progress_queue=queue.Queue(),
        last_progress_msg=[None], repeat_count=[0], progress_grouping=grouping,
        stream_consumer_holder=[None], _cleanup_progress=False,
    )
    return runner, runner._progress_edit_state(adapter)


def _tool_line(runner, state, text="Reading files"):
    runner._progress_emit(text)
    return runner._progress_absorb(state, runner._ctx.progress_queue.get_nowait())


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [0, 10])
async def test_tool_progress_edits_follow_daily_budget_without_restoring_typing(tmp_path, used):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(adapter._daily_quota(), used)
    runner, state = _progress_runner(adapter)
    _tool_line(runner, state)
    _tool_line(runner, state)  # dedup must preserve the real-tool marker
    token = bind_trigger(SimpleNamespace(text="show me the tool progress"))
    try:
        await runner._progress_send_or_edit(state, state.progress_lines[-1])
        assert state.progress_msg_id
        edited = await runner._edit_progress_message(state, state.progress_msg_id, "Reading files\nRunning checks")
        assert edited.success
        assert bool((edited.raw_response or {}).get("skipped")) is bool(used)
        adapter._bot.send_message.assert_awaited_once()
        assert adapter._bot.edit_message_text.await_count == (0 if used else 1)
        if used:
            await adapter.send_typing(CHAT)
            adapter._bot.send_chat_action.assert_not_awaited()
    finally:
        reset_trigger(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [0, 10])
@pytest.mark.parametrize("path", ["reset", "overflow", "separate"])
async def test_only_first_tool_bubble_is_protected_even_if_it_precedes_pressure(tmp_path, used, path):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(adapter._daily_quota(), used)
    runner, state = _progress_runner(adapter, "separate" if path == "separate" else "edit")
    token = bind_trigger(SimpleNamespace(text="run the checks"))
    try:
        await runner._progress_send_or_edit(state, _tool_line(runner, state))
        assert state.progress_msg_id
        _spend_calls(adapter._daily_quota(), 10)
        if path == "reset":
            runner._reset_progress_bubble(state)
            runner._ctx._progress_metadata = {"thread_id": "43"}
        elif path == "overflow":
            state._PROGRESS_TEXT_LIMIT = len(state.progress_lines[0])

        for i in range(3):
            line = _tool_line(runner, state, f"Checking step {i}")
            if path == "overflow":
                assert await runner._roll_progress_overflow_if_needed(state)
            else:
                await runner._progress_send_or_edit(state, line)
        adapter._bot.send_message.assert_awaited_once()
    finally:
        reset_trigger(token)


@pytest.mark.asyncio
async def test_permanent_edit_failure_does_not_rearm_tool_bubble_under_pressure(tmp_path):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger
    from telegram.error import BadRequest

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    runner, state = _progress_runner(adapter)

    async def dead_bubble(**_kwargs):
        _spend_calls(adapter._daily_quota(), 10)
        raise BadRequest("Message to edit not found")

    adapter._bot.edit_message_text.side_effect = dead_bubble
    token = bind_trigger(SimpleNamespace(text="run the checks"))
    try:
        await runner._progress_send_or_edit(state, _tool_line(runner, state))
        await runner._progress_send_or_edit(state, _tool_line(runner, state, "Running checks"))
        assert state.reanchored and state.progress_msg_id is None
        await runner._progress_send_or_edit(state, _tool_line(runner, state, "Finishing checks"))
        adapter._bot.edit_message_text.assert_awaited_once()
        adapter._bot.send_message.assert_awaited_once()
    finally:
        reset_trigger(token)


@pytest.mark.asyncio
async def test_normal_budget_keeps_continuations_and_separate_tool_sends(tmp_path):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=100, profile_dir=tmp_path)
    token = bind_trigger(SimpleNamespace(text="run the checks"))
    try:
        runner, state = _progress_runner(adapter)
        await runner._progress_send_or_edit(state, _tool_line(runner, state))
        runner._reset_progress_bubble(state)
        await runner._progress_send_or_edit(state, _tool_line(runner, state, "Running checks"))
        state._PROGRESS_TEXT_LIMIT = len(state.progress_lines[0])
        _tool_line(runner, state, "Checking another file")
        assert await runner._roll_progress_overflow_if_needed(state)
        state.can_edit = False
        await runner._progress_send_or_edit(state, _tool_line(runner, state, "Finishing checks"))
        assert adapter._bot.send_message.await_count == 4
    finally:
        reset_trigger(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [0, 10])
async def test_initial_tool_bubble_does_not_exempt_adapter_split_continuations(tmp_path, used):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(adapter._daily_quota(), used)
    runner, state = _progress_runner(adapter)
    token = bind_trigger(SimpleNamespace(text="show the verbose tool arguments"))
    try:
        await runner._progress_send_or_edit(state, _tool_line(runner, state, "Reading " + "x" * 9000))
        assert state.progress_msg_id
        assert (adapter._bot.send_message.await_count == 1) is bool(used)
        assert not used or state.tool_progress_sent
    finally:
        reset_trigger(token)


@pytest.mark.asyncio
async def test_partial_initial_tool_send_also_spends_the_protected_slot(tmp_path, monkeypatch):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger
    from telegram.error import RetryAfter

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    adapter._bot.send_message.side_effect = [SimpleNamespace(message_id=1), RetryAfter(600)]
    runner, state = _progress_runner(adapter)
    token = bind_trigger(SimpleNamespace(text="show the verbose tool arguments"))
    try:
        partial = await runner._send_progress_text(state, _tool_line(runner, state, "Reading " + "x" * 9000))
        assert not partial.success and partial.raw_response["partial_overflow"]
        assert state.tool_progress_sent
        _spend_calls(adapter._daily_quota(), 10)
        monkeypatch.setattr(adapter, "_send_flood_cooldown_remaining", lambda _chat: None)
        runner._reset_progress_bubble(state)
        await runner._progress_send_or_edit(state, _tool_line(runner, state, "Continuing checks"))
        assert adapter._bot.send_message.await_count == 2
    finally:
        reset_trigger(token)


@pytest.mark.asyncio
async def test_failed_initial_tool_send_can_retry_but_only_accepts_one_bubble(tmp_path, monkeypatch):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(adapter._daily_quota(), 10)
    runner, state = _progress_runner(adapter, "separate")
    token = bind_trigger(SimpleNamespace(text="run the checks"))
    try:
        monkeypatch.setattr(adapter, "_send_flood_cooldown_remaining", lambda _chat: 600)
        refused = await runner._send_progress_text(state, _tool_line(runner, state))
        assert not refused.success and refused.retry_after == 600
        assert not state.tool_progress_sent
        adapter._bot.send_message.assert_not_awaited()
        monkeypatch.setattr(adapter, "_send_flood_cooldown_remaining", lambda _chat: None)
        for i in range(3):
            await runner._progress_send_or_edit(state, _tool_line(runner, state, f"Checking step {i}"))
        adapter._bot.send_message.assert_awaited_once()
        next_turn, next_state = _progress_runner(adapter, "separate")
        await next_turn._progress_send_or_edit(next_state, _tool_line(next_turn, next_state))
        assert adapter._bot.send_message.await_count == 2
    finally:
        reset_trigger(token)


@pytest.mark.asyncio
async def test_deferred_initial_tool_send_spends_slot_and_preserves_native_replay_class(tmp_path, monkeypatch):
    import gateway.outbox as outbox
    from gateway.platforms.base import OUTBOUND_TOOL_PROGRESS
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(durable_outbox_enabled=True))
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(adapter._daily_quota(), 10)
    clock = Wall(_utc("2026-10-25 12:00:00"))
    monkeypatch.setattr(outbox.time, "time", clock)
    monkeypatch.setattr(outbox, "_schedule_retry", AsyncMock())
    monkeypatch.setattr(adapter, "_send_flood_cooldown_remaining", lambda _chat: 600)
    runner, state = _progress_runner(adapter, "separate")
    token = bind_trigger(SimpleNamespace(text="run the checks"))
    outbox.bind_turn(tmp_path, "typed-tool-turn")
    try:
        deferred = await runner._send_progress_text(state, _tool_line(runner, state))
        assert deferred.success and deferred.deferred and deferred.message_id is None
        assert state.tool_progress_sent
        monkeypatch.setattr(adapter, "_send_flood_cooldown_remaining", lambda _chat: None)
        runner._reset_progress_bubble(state)
        await runner._send_progress_text(state, _tool_line(runner, state, "Running checks"))
        await runner._edit_progress_message(state, "1", "Checking results")
        adapter._bot.send_message.assert_not_awaited()
        outbox.clear_turn()
        clock.t += 601
        store = outbox.Outbox(tmp_path)
        rows = store.all_rows()
        assert [(row.type, row.payload["_outbound_class"]) for row in rows] == [
            ("send", OUTBOUND_TOOL_PROGRESS), ("send", OUTBOUND_PROGRESS), ("edit_message", OUTBOUND_PROGRESS),
        ]
        assert all(row.payload["metadata"] == {"thread_id": "42"} for row in rows)
        assert [row.state for row in rows] == ["pending", "failed_unsent", "delivered"]
        for row in rows[1:]:
            store.enqueue("recovery-check", row.type, row.payload)
        # Native replay restores each persisted class, not the class of the recovery caller.
        with outbound_class(OUTBOUND_NOTICE):
            await outbox.recover(store, adapter)
        adapter._bot.send_message.assert_awaited_once()
        adapter._bot.edit_message_text.assert_not_awaited()
        assert not store.pending()
        assert not store.ambiguous()
    finally:
        outbox.clear_turn()
        reset_trigger(token)


@pytest.mark.parametrize("trigger", ["goal", "loop", "cron", "untagged"])
def test_background_tool_progress_keeps_daily_message_and_call_limits(tmp_path, trigger):
    from gateway.platforms.base import OUTBOUND_TOOL_PROGRESS

    quota = DailyQuota(soft_ceiling=10, call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(quota, 10)
    for _ in range(10):
        quota.record(CHAT, "sendMessage")
    assert quota.sheds(CHAT, OUTBOUND_TOOL_PROGRESS, trigger)
    assert not quota.sheds(CHAT, OUTBOUND_TOOL_PROGRESS, "typed")

    import json
    from gateway.outbox import _outbound_class_for_payload, _replay_outbound_class
    with outbound_class(OUTBOUND_TOOL_PROGRESS):
        payload = {"metadata": {"thread_id": "42"}, "_outbound_class": _outbound_class_for_payload({})}
    replayed = _replay_outbound_class(json.loads(json.dumps(payload)))
    assert replayed == OUTBOUND_TOOL_PROGRESS
    assert quota.sheds(CHAT, replayed, trigger)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    SimpleNamespace(text="[Continuing toward your standing goal]"),
    SimpleNamespace(text="[/loop wakeup]"),
    SimpleNamespace(text="scheduled cron work", internal=True),
    None,
])
async def test_background_tool_bubbles_still_shed_through_runner_and_adapter(tmp_path, event):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(soft_ceiling=10, call_soft_ceiling=10, profile_dir=tmp_path)
    for _ in range(10):
        adapter._daily_quota().record(CHAT, "sendMessage")
    runner, state = _progress_runner(adapter)
    token = bind_trigger(event) if event else None
    try:
        await runner._progress_send_or_edit(state, _tool_line(runner, state))
        assert not state.tool_progress_sent
        adapter._bot.send_message.assert_not_awaited()
        assert (await adapter.send(CHAT, "The final answer")).success
        adapter._bot.send_message.assert_awaited_once()
    finally:
        if token is not None:
            reset_trigger(token)


@pytest.mark.asyncio
async def test_thinking_only_progress_still_sheds_at_daily_call_ceiling(tmp_path):
    from plugins.platforms.telegram.chat_budget import bind_trigger, reset_trigger

    adapter = _adapter(tmp_path)
    adapter._telegram_daily_quota = DailyQuota(call_soft_ceiling=10, profile_dir=tmp_path)
    _spend_calls(adapter._daily_quota(), 10)
    runner, state = _progress_runner(adapter)
    runner._progress_absorb(state, "Thinking about the answer")
    token = bind_trigger(SimpleNamespace(text="explain it"))
    try:
        sent = await runner._send_progress_text(state, state.progress_lines[0])
        assert sent.error == "daily_budget_shed"
        adapter._bot.send_message.assert_not_awaited()
        adapter._bot.edit_message_text.assert_not_awaited()
        await runner._progress_send_or_edit(state, _tool_line(runner, state))
        adapter._bot.send_message.assert_awaited_once()
    finally:
        reset_trigger(token)
