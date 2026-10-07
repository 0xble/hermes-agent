"""Per-chat daily Bot API volume ledger (measurement for the 2026-10-05 34507s ban)."""

import datetime as dt
import logging
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram import daily_volume as dv
from plugins.platforms.telegram.chat_budget import ChatBudgetRateLimiter, ChatOutboundBudget
from plugins.platforms.telegram.daily_volume import DailyVolume

CHAT = "2027045491"


class Wall:
    def __init__(self, t: float):
        self.t = t

    def __call__(self):
        return self.t


def _utc(s: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()


def test_counts_every_endpoint_and_each_created_message(tmp_path):
    volume = DailyVolume(profile_dir=tmp_path, wall=Wall(_utc("2026-10-05 12:00:00")))
    volume.record(CHAT, "sendMessage")
    volume.record(CHAT, "editMessageText")
    volume.record(CHAT, "sendChatAction")
    volume.record(CHAT, "deleteMessages", {"message_ids": [1, 2, 3]})
    volume.record(CHAT, "sendMediaGroup", {"media": [{}, {}, {}]})
    assert volume.messages(CHAT) == 4  # one text plus three album items; edits and actions create none
    assert volume.calls(CHAT) == {"sendMessage": 1, "editMessageText": 1, "sendChatAction": 1,
                                  "deleteMessages": 1, "sendMediaGroup": 1}


def test_window_rolls_at_utc_midnight_by_default(tmp_path):
    wall = Wall(_utc("2026-10-05 23:59:00"))
    volume = DailyVolume(profile_dir=tmp_path, wall=wall)
    volume.record(CHAT, "sendMessage")
    wall.t = _utc("2026-10-06 00:00:01")
    assert volume.messages(CHAT) == 0


def test_counts_survive_a_restart_and_include_the_standalone_lane(tmp_path):
    wall = Wall(_utc("2026-10-05 12:00:00"))
    gateway = DailyVolume(profile_dir=tmp_path, wall=wall)
    for _ in range(5):
        gateway.record(CHAT, "sendMessage")
    gateway.flush()
    standalone = DailyVolume(profile_dir=tmp_path, wall=wall, flush_interval=0.0)
    standalone.record(CHAT, "sendMessage")
    standalone.record(CHAT, "sendMessage")
    gateway.flush()  # reads the shared total back
    assert gateway.messages(CHAT) == 7
    assert DailyVolume(profile_dir=tmp_path, wall=wall).messages(CHAT) == 7


def test_long_penalty_anchors_the_window_at_the_reset_and_logs_the_volume(tmp_path, caplog):
    """17:23:52 PDT + 34507s = 09:58:59 UTC, the reset the evidence points at."""
    wall = Wall(_utc("2026-10-06 00:23:52"))
    volume = DailyVolume(profile_dir=tmp_path, wall=wall)
    for _ in range(3):
        volume.record(CHAT, "sendMessage")
    volume.record(CHAT, "editMessageText")
    caplog.set_level(logging.WARNING, logger=dv.__name__)
    volume.note_retry_after(CHAT, 34507.0)
    assert any("refused for 34507s after 3 messages and 4 calls" in r.getMessage() for r in caplog.records)
    assert volume.messages(CHAT) == 3  # this traffic belongs to the window that ends at the reset
    wall.t = _utc("2026-10-06 09:58:58")
    assert volume.messages(CHAT) == 3
    wall.t = _utc("2026-10-06 09:59:00")
    assert volume.messages(CHAT) == 0
    restarted = DailyVolume(profile_dir=tmp_path, wall=wall)
    restarted.record(CHAT, "sendMessage")
    wall.t = _utc("2026-10-07 09:58:58")
    assert restarted.messages(CHAT) == 1  # the anchor persisted


def test_short_penalty_logs_but_does_not_move_the_window(tmp_path, caplog):
    wall = Wall(_utc("2026-10-05 12:00:00"))
    volume = DailyVolume(profile_dir=tmp_path, wall=wall)
    volume.record(CHAT, "sendMessage")
    caplog.set_level(logging.WARNING, logger=dv.__name__)
    volume.note_retry_after(CHAT, 3.0)
    assert any("refused for 3s" in r.getMessage() for r in caplog.records)
    wall.t = _utc("2026-10-06 00:00:01")
    assert volume.messages(CHAT) == 0


def test_volume_is_logged_hourly(tmp_path, caplog):
    wall = Wall(_utc("2026-10-05 12:00:00"))
    volume = DailyVolume(profile_dir=tmp_path, wall=wall)
    caplog.set_level(logging.INFO, logger=dv.__name__)
    volume.record(CHAT, "sendMessage")
    assert not [r for r in caplog.records if "daily volume since" in r.getMessage()]
    wall.t += dv.LOG_INTERVAL_SECS
    volume.record(CHAT, "editMessageText")
    lines = [r.getMessage() for r in caplog.records if "daily volume since" in r.getMessage()]
    assert lines and "1 messages, 2 calls" in lines[0] and '"editMessageText":1' in lines[0]


@pytest.mark.asyncio
async def test_limiter_records_every_wire_call_and_the_refusal(tmp_path):
    from telegram.error import RetryAfter

    volume = DailyVolume(profile_dir=tmp_path)
    limiter = ChatBudgetRateLimiter(ChatOutboundBudget(gap_override=0), volume=volume)
    wire = AsyncMock(return_value=True)
    for _ in range(3):
        await limiter.process_request(wire, (), {}, "sendMessage", {"chat_id": CHAT}, None)
    await limiter.process_request(wire, (), {}, "getChat", {"chat_id": CHAT}, None)  # reads are not metered
    await limiter.process_request(wire, (), {}, "answerCallbackQuery", {"callback_query_id": "q"}, None)

    async def banned():
        raise RetryAfter(dt.timedelta(seconds=34507))

    with pytest.raises(RetryAfter):
        await limiter.process_request(banned, (), {}, "editMessageText", {"chat_id": CHAT}, None)
    assert volume.calls(CHAT) == {"sendMessage": 3, "editMessageText": 1}
    assert volume.messages(CHAT) == 3


def test_gateway_and_standalone_lanes_carry_a_ledger(tmp_path, monkeypatch):
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.config import PlatformConfig
    from tools import send_message_senders as senders

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._update_receipt_dir = tmp_path
    assert isinstance(adapter._chat_rate_limiter().volume, DailyVolume)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    assert isinstance(senders._standalone_telegram_rate_limiter().volume, DailyVolume)
