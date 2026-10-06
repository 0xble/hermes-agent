"""Shared per-chat Telegram outbound budget and ingress pacing regressions (#107612)."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram import chat_budget as cb


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def simulated_sleep(monkeypatch):
    clock = _Clock()

    async def fake_sleep(seconds):
        clock.t += max(0.0, seconds)

    monkeypatch.setattr(cb.asyncio, "sleep", fake_sleep)
    return clock


def _adapter(**bot_methods):
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


def test_chat_class_and_headroom():
    assert not cb.is_group_chat(2027045491)
    assert cb.is_group_chat(-1001234567890)
    assert cb.is_group_chat("@somechannel")
    assert 60.0 / cb.PRIVATE_GAP_SECS <= 0.75 * cb.PRIVATE_CEILING_PER_MIN + 1e-9
    assert 60.0 / cb.GROUP_GAP_SECS <= 0.75 * cb.GROUP_CEILING_PER_MIN + 1e-9
    assert cb.PRIVATE_TYPING_GAP_SECS >= cb.PRIVATE_GAP_SECS
    assert cb.GROUP_TYPING_GAP_SECS >= cb.GROUP_GAP_SECS


@pytest.mark.asyncio
async def test_request_layer_shares_one_slot_and_sheds_cosmetic(simulated_sleep):
    budget = cb.ChatOutboundBudget(clock=simulated_sleep)
    limiter = cb.ChatBudgetRateLimiter(budget)
    fired = []

    async def callback():
        fired.append(simulated_sleep.t)
        return True

    await limiter.process_request(callback, (), {}, "sendMessage", {"chat_id": 123}, None)
    await limiter.process_request(callback, (), {}, "sendChatAction", {"chat_id": 123}, {"kind": cb.KIND_TYPING})
    await limiter.process_request(callback, (), {}, "editMessageText", {"chat_id": 123}, {"kind": cb.KIND_INTERIM})
    await limiter.process_request(callback, (), {}, "sendMessage", {"chat_id": 123}, None)
    assert len(fired) == 2
    assert limiter.shed_count == 2


@pytest.mark.asyncio
async def test_all_endpoints_share_class_ceiling(simulated_sleep):
    limiter = cb.ChatBudgetRateLimiter(cb.ChatOutboundBudget(clock=simulated_sleep))
    fired = []

    async def callback():
        fired.append(simulated_sleep.t)
        return True

    endpoints = ["sendMessage", "editMessageText", "sendChatAction", "sendMessageDraft",
                 "deleteMessages", "editForumTopic", "setMessageReaction", "sendPhoto"]
    start = simulated_sleep.t
    while simulated_sleep.t - start < 60:
        for endpoint in endpoints:
            kind = cb.KIND_TYPING if endpoint == "sendChatAction" else (
                cb.KIND_INTERIM if "Draft" in endpoint else None)
            await limiter.process_request(callback, (), {}, endpoint, {"chat_id": "-1001"},
                                          {"kind": kind} if kind else None)
        simulated_sleep.t += 0.05
    assert len([t for t in fired if t - start < 60]) <= 0.75 * cb.GROUP_CEILING_PER_MIN + 1
    assert limiter.shed_count > 0


@pytest.mark.asyncio
async def test_retry_after_widens_only_one_chat_and_expires(simulated_sleep):
    from telegram.error import RetryAfter

    budget = cb.ChatOutboundBudget(clock=simulated_sleep)
    recorded = []
    limiter = cb.ChatBudgetRateLimiter(budget, on_retry_after=lambda key, wait: recorded.append((key, wait)))
    base = budget.gap("111")

    async def refused():
        raise RetryAfter(dt.timedelta(seconds=0.6))

    with pytest.raises(RetryAfter):
        await limiter.process_request(refused, (), {}, "sendMessage", {"chat_id": 111}, None)
    assert recorded == [("111", 0.6)]
    assert budget.gap("111") == pytest.approx(base * cb.PENALTY_FACTOR)
    assert budget.gap("222") == pytest.approx(base)
    simulated_sleep.t += cb.PENALTY_WINDOW_SECS + 1
    assert budget.gap("111") == pytest.approx(base)


@pytest.mark.asyncio
async def test_standalone_metered_bot_uses_the_same_request_gate(simulated_sleep):
    inner = SimpleNamespace(send_message=AsyncMock(return_value=True))
    bot = cb.MeteredBot(inner, cb.ChatBudgetRateLimiter(cb.ChatOutboundBudget(clock=simulated_sleep)))
    await bot.send_message(chat_id=123, text="one")
    await bot.send_message(chat_id=123, text="two")
    assert inner.send_message.await_count == 2
    assert simulated_sleep.t > 1000.0


@pytest.mark.asyncio
async def test_known_adapter_cooldown_is_refused_without_request():
    callback = AsyncMock(return_value=True)
    limiter = cb.ChatBudgetRateLimiter(cb.ChatOutboundBudget(), penalty_remaining=lambda key: 9.0)
    from telegram.error import RetryAfter

    with pytest.raises(RetryAfter):
        await limiter.process_request(callback, (), {}, "editForumTopic", {"chat_id": 123}, None)
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_typing_loops_share_one_chat_budget():
    limiter = cb.ChatBudgetRateLimiter(cb.ChatOutboundBudget())
    callback = AsyncMock(return_value=True)
    await asyncio.gather(*(limiter.process_request(
        callback, (), {}, "sendChatAction", {"chat_id": 12345}, {"kind": cb.KIND_TYPING}) for _ in range(30)))
    assert callback.await_count == 1
    assert limiter.shed_count == 29


@pytest.mark.asyncio
async def test_interim_edit_is_shed_and_final_edit_waits():
    adapter = _adapter()
    assert (await adapter.edit_message("12345", "900", "one", finalize=False)).success
    skipped = await adapter.edit_message("12345", "900", "two", finalize=False)
    assert skipped.raw_response == {"skipped": True}
    sleeps = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    # Adapter-local fake path uses the compatibility reservation; production PTB uses the limiter.
    import plugins.platforms.telegram.adapter as adapter_module
    original = adapter_module.asyncio.sleep
    adapter_module.asyncio.sleep = record_sleep
    try:
        final = await adapter.edit_message("12345", "900", "two final", finalize=True)
    finally:
        adapter_module.asyncio.sleep = original
    assert final.success
    assert adapter._bot.edit_message_text.await_count == 2


@pytest.mark.asyncio
async def test_batch_cleanup_uses_at_most_hundred_ids_per_request():
    adapter = _adapter(delete_messages=AsyncMock(return_value=True))
    ids = [str(n) for n in range(1, 151)]
    outcome = await adapter.delete_messages("12345", ids)
    assert adapter._bot.delete_messages.await_count == 2
    assert all(outcome[mid] for mid in ids)
