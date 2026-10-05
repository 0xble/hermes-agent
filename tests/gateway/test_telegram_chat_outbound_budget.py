"""One per-chat Telegram outbound budget shared by every Bot API call (#107612, #99643).

On 2026-10-04 a busy private chat took 5464s and then 24237s flood bans: a 1.0s send+interim-edit
slot (60/min alone) sat beside unmetered typing loops (one per active session), final edits,
drafts, deletions and topic edits. These tests pin the SUM across every path against the chat
class's ceiling, not any one limiter in isolation.
"""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _sim(monkeypatch, clock):
    """Run the budget on simulated time: a pacing sleep advances the fake clock."""
    from plugins.platforms.telegram import chat_budget

    async def fake_sleep(seconds):
        clock.t += max(0.0, seconds)

    monkeypatch.setattr(chat_budget.asyncio, "sleep", fake_sleep)


def _adapter(**bot_methods) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._rich_messages_enabled = False
    adapter._rich_send_disabled = True
    adapter._bot = MagicMock()
    adapter._bot.send_chat_action = AsyncMock(return_value=True)
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    adapter._bot.edit_message_text = AsyncMock(return_value=MagicMock())
    for name, value in bot_methods.items():
        setattr(adapter._bot, name, value)
    return adapter


@pytest.mark.asyncio
async def test_concurrent_typing_loops_share_one_chat_budget():
    """Thirty sessions' typing loops in one private chat make ONE chat action, not thirty: typing is
    budgeted per chat and shed when that budget is spent, never per session or queued."""
    adapter = _adapter()
    await asyncio.gather(*(adapter.send_typing("12345", metadata={"thread_id": str(n)}) for n in range(30)))
    assert adapter._bot.send_chat_action.await_count == 1


def test_documented_rates_leave_headroom_under_each_class_ceiling():
    from plugins.platforms.telegram import chat_budget as cb

    assert 60.0 / cb.PRIVATE_GAP_SECS <= 0.75 * cb.PRIVATE_CEILING_PER_MIN + 1e-9
    assert 60.0 / cb.GROUP_GAP_SECS <= 0.75 * cb.GROUP_CEILING_PER_MIN + 1e-9
    # Typing and interim previews are SHARES of the one slot, never extra budget.
    assert cb.PRIVATE_TYPING_GAP_SECS >= cb.PRIVATE_GAP_SECS and cb.GROUP_TYPING_GAP_SECS >= cb.GROUP_GAP_SECS
    assert cb.EDIT_FLOOR_SECS >= cb.PRIVATE_GAP_SECS


def test_chat_class_comes_from_the_id():
    from plugins.platforms.telegram.chat_budget import ChatOutboundBudget, is_group_chat

    assert not is_group_chat(2027045491) and not is_group_chat("2027045491")
    assert is_group_chat(-1001234567890) and is_group_chat("-42") and is_group_chat("@somechannel")
    budget = ChatOutboundBudget()
    assert budget.gap("-1001234567890") > budget.gap("2027045491")


@pytest.mark.parametrize("chat_id, ceiling_attr", [("2027045491", "PRIVATE_CEILING_PER_MIN"),
                                                    ("-1001234567890", "GROUP_CEILING_PER_MIN")])
@pytest.mark.asyncio
async def test_every_path_together_stays_under_the_class_ceiling(monkeypatch, chat_id, ceiling_attr):
    """Saturate one chat for a simulated minute with every kind of call at once (deliveries, final and
    interim edits, typing, drafts, deletions, topic edits) and count what reaches Telegram."""
    from plugins.platforms.telegram import chat_budget as cb

    clock = _Clock()
    _sim(monkeypatch, clock)
    limiter = cb.ChatBudgetRateLimiter(cb.ChatOutboundBudget(clock=clock))
    fired: list[float] = []

    async def callback():
        fired.append(clock.t)
        return True

    endpoints = ["sendMessage", "editMessageText", "sendChatAction", "sendMessageDraft", "deleteMessages",
                 "editForumTopic", "setMessageReaction", "sendRichMessageDraft", "sendPhoto"]
    start = clock.t
    while clock.t - start < 60.0:
        for endpoint in endpoints:
            await limiter.process_request(callback, (), {}, endpoint, {"chat_id": chat_id}, None)
        clock.t += 0.05  # producers retry every 50ms on top of any pacing wait
    in_window = [t for t in fired if t - start < 60.0]
    assert len(in_window) <= 0.75 * getattr(cb, ceiling_attr) + 1
    assert limiter.shed_count > 0  # cosmetic traffic was dropped, not queued


@pytest.mark.asyncio
async def test_reads_and_chatless_calls_are_not_metered(monkeypatch):
    from plugins.platforms.telegram import chat_budget as cb

    clock = _Clock()
    _sim(monkeypatch, clock)
    budget = cb.ChatOutboundBudget(clock=clock)
    limiter = cb.ChatBudgetRateLimiter(budget)
    callback = AsyncMock(return_value={})
    for _ in range(5):
        await limiter.process_request(callback, (), {}, "getChat", {"chat_id": 7}, None)
        await limiter.process_request(callback, (), {}, "answerCallbackQuery", {"callback_query_id": "x"}, None)
    assert callback.await_count == 10 and budget.remaining(7) == 0


@pytest.mark.asyncio
async def test_published_retry_after_widens_only_that_chat_and_expires(monkeypatch):
    from telegram.error import RetryAfter

    from plugins.platforms.telegram import chat_budget as cb

    clock = _Clock()
    _sim(monkeypatch, clock)
    budget = cb.ChatOutboundBudget(clock=clock)
    recorded = []
    limiter = cb.ChatBudgetRateLimiter(budget, on_retry_after=lambda key, wait: recorded.append((key, wait)))
    base = budget.gap("111")

    async def refused():
        raise RetryAfter(dt.timedelta(seconds=0.6))

    with pytest.raises(RetryAfter):
        await limiter.process_request(refused, (), {}, "sendMessage", {"chat_id": 111}, None)
    assert recorded == [("111", 0.6)]
    # The refused call's own retry waits at most the routine gap: the multiplier applies from the
    # NEXT call, so it never inflates the retry the server just timed.
    assert 0.6 <= budget.remaining("111") <= base + 1e-9
    assert budget.gap("111") == pytest.approx(base * cb.PENALTY_FACTOR)
    assert budget.gap("222") == pytest.approx(base)  # scoped to the offending chat
    clock.t += cb.PENALTY_WINDOW_SECS + 1
    assert budget.gap("111") == pytest.approx(base)  # expires on its own


@pytest.mark.asyncio
async def test_known_server_penalty_is_refused_without_a_request(tmp_path):
    """A 7728s penalty already on record stops every path at the request layer, whichever adapter
    method issued it (topic edits and reactions never checked the window themselves)."""
    from telegram.error import RetryAfter

    from plugins.platforms.telegram import flood_state

    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    flood_state.record_deadline(tmp_path, "2027045491", 7728.0)
    callback = AsyncMock(return_value=True)
    limiter = adapter._chat_rate_limiter()
    for endpoint in ("editForumTopic", "setMessageReaction", "sendMessage"):
        with pytest.raises(RetryAfter) as caught:
            await limiter.process_request(callback, (), {}, endpoint, {"chat_id": 2027045491}, None)
        assert caught.value.retry_after.total_seconds() > 7000
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_deliveries_share_the_slot_and_are_never_dropped():
    from plugins.platforms.telegram import chat_budget as cb

    budget = cb.ChatOutboundBudget(gap_override=0.05)
    limiter = cb.ChatBudgetRateLimiter(budget)
    loop = asyncio.get_running_loop()
    fired: list[float] = []

    async def callback():
        fired.append(loop.time())
        return {"ok": True}

    await asyncio.gather(*(limiter.process_request(callback, (), {}, "sendMessage", {"chat_id": 5}, None)
                           for _ in range(6)))
    assert len(fired) == 6
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    assert min(gaps) >= 0.05 - 0.01


@pytest.mark.asyncio
async def test_routine_group_spacing_is_waited_out_but_a_long_penalty_is_handed_back():
    """The inline-wait cap exists to refuse sleeping off a multi-hour penalty; it is floored at the
    chat's own gap so a penalised group's ordinary spacing never surfaces as a flood error."""
    from plugins.platforms.telegram.adapter import _FLOOD_INLINE_WAIT_CAP_SECS

    adapter = _adapter()
    group = "-1001234567890"
    adapter._chat_budget().note_retry_after(group, 0.5)
    widened = adapter._chat_budget().gap(group)
    assert widened > _FLOOD_INLINE_WAIT_CAP_SECS
    assert adapter._flood_inline_wait_cap(group) >= widened
    assert adapter._flood_inline_wait_cap(group) < 7728.0


@pytest.mark.asyncio
async def test_interim_edits_respect_the_edit_floor_and_finals_wait_for_their_slot(monkeypatch):
    adapter = _adapter()
    chat = "2027045491"
    assert (await adapter.edit_message(chat, "900", "one", finalize=False)).success
    skipped = await adapter.edit_message(chat, "900", "two", finalize=False)
    assert skipped.raw_response == {"skipped": True}
    assert adapter._bot.edit_message_text.await_count == 1

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    final = await adapter.edit_message(chat, "900", "two final", finalize=True)
    assert final.success and adapter._bot.edit_message_text.await_count == 2
    assert sleeps and sleeps[0] > 0  # the final edit waited for the slot instead of firing past it


def test_progress_producer_is_no_faster_than_the_transport_edit_floor():
    from gateway.run_turn_runner import TurnRunner
    from plugins.platforms.telegram.chat_budget import EDIT_FLOOR_SECS

    assert TurnRunner._PROGRESS_EDIT_INTERVAL >= EDIT_FLOOR_SECS


@pytest.mark.asyncio
async def test_bubble_cleanup_uses_one_batch_request_per_hundred_ids():
    adapter = _adapter(delete_messages=AsyncMock(return_value=True))
    ids = [str(n) for n in range(1, 151)]
    outcome = await adapter.delete_messages("2027045491", ids)
    assert adapter._bot.delete_messages.await_count == 2
    assert all(outcome[mid] for mid in ids)


@pytest.mark.asyncio
async def test_batch_cleanup_flood_refusal_keeps_every_id_for_retry(tmp_path):
    from telegram.error import RetryAfter

    adapter = _adapter(delete_messages=AsyncMock(side_effect=RetryAfter(dt.timedelta(seconds=900))))
    adapter._update_receipt_dir = tmp_path
    outcome = await adapter.delete_messages("2027045491", ["1", "2", "3"])
    assert outcome == {"1": False, "2": False, "3": False}
    assert adapter._send_flood_cooldown_remaining("2027045491") > 800


@pytest.mark.asyncio
async def test_standalone_lane_refuses_inside_a_recorded_penalty(tmp_path, monkeypatch):
    """Cron's standalone fallback must not spend requests (or sleep for hours) inside a penalty the
    gateway is already waiting out."""
    from telegram.error import RetryAfter

    from plugins.platforms.telegram import flood_state
    from plugins.platforms.telegram.chat_budget import MeteredBot
    from tools import send_message_senders as senders

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    flood_state.record_deadline(tmp_path, "2027045491", 24237.0)
    inner = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    bot = MeteredBot(inner, senders._standalone_telegram_rate_limiter())
    with pytest.raises(RetryAfter):
        await bot.send_message(chat_id=2027045491, text="hi")
    inner.send_message.assert_not_awaited()
    assert senders._telegram_retry_delay(RetryAfter(dt.timedelta(seconds=24237)), 0) is None
    assert senders._telegram_retry_delay(RetryAfter(dt.timedelta(seconds=2)), 0) == pytest.approx(2.0)
