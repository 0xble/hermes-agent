"""Daily per-chat Telegram call counter (measurement only).

The 2026-10-04/05 bans hit while the chat ran 0-6 calls/min, inside the per-minute budget, and both
ended near 09:59 UTC. The counter records every call that reaches Telegram, by chat, endpoint and
trigger, persists it, and logs the window at a long retry_after so the volume threshold is measured.
"""

import asyncio
import datetime as dt
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram import chat_budget
from plugins.platforms.telegram.chat_budget import (
    ChatBudgetRateLimiter, ChatOutboundBudget, DailyCallCounter, bind_trigger, classify_trigger,
    current_trigger, reset_trigger)


class _Wall:
    def __init__(self, t=1_791_200_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _limiter(tmp_path, wall, **kwargs):
    counter = DailyCallCounter(tmp_path, wall=wall, flush_interval=60.0, log_interval=3600.0)
    return ChatBudgetRateLimiter(ChatOutboundBudget(gap_override=0.0), counter=counter, **kwargs), counter


async def _call(limiter, endpoint, chat="2027045491", result="ok"):
    async def callback():
        if isinstance(result, BaseException):
            raise result
        return result
    return await limiter.process_request(callback, (), {}, endpoint, {"chat_id": chat}, None)


@pytest.mark.parametrize("text,internal,expected", [
    ("hello", False, "typed"),
    ("[Continuing toward your standing goal] Goal: x", True, "goal"),
    ("[/loop wakeup #3, every 3h] Recurring task", True, "loop"),
    ("[relay from=hermes:default/abc receipt=1]", False, "relay"),
    ("[relay · chain=1 · hop=1]", True, "relay"),
    ("[IMPORTANT: Background process proc_1 completed normally", True, "process"),
    ("[IMPORTANT: 5 background processes completed for this session", True, "process"),
    ("[ASYNC DELEGATION BATCH COMPLETE — deleg_1]", True, "delegation"),
    ("[IMPORTANT: 2 background subagent delegations completed", True, "delegation"),
    ("[System note: Resume the pending turn. Any restart, update, or shutdown command has already run", True, "restart"),
    ("something else", True, "internal"),
])
def test_classify_trigger(text, internal, expected):
    assert classify_trigger(SimpleNamespace(text=text, internal=internal)) == expected


def test_trigger_label_is_scoped_to_the_context():
    assert current_trigger() == "untagged"
    token = bind_trigger(SimpleNamespace(text="[/loop wakeup #1]", internal=True))
    try:
        assert current_trigger() == "loop"
    finally:
        reset_trigger(token)
    assert current_trigger() == "untagged"


def test_counts_by_endpoint_and_trigger_and_persists_across_instances(tmp_path):
    wall = _Wall()
    limiter, counter = _limiter(tmp_path, wall)

    async def run():
        token = bind_trigger(SimpleNamespace(text="[Continuing toward your standing goal]", internal=True))
        try:
            await _call(limiter, "sendMessage")
            await _call(limiter, "editMessageText")
            await asyncio.create_task(_call(limiter, "deleteMessage"))  # spawned tasks inherit the label
        finally:
            reset_trigger(token)
        await _call(limiter, "editMessageText")  # untagged (cron, outbox replay, housekeeping)
        await _call(limiter, "getChat")  # reads are not metered and not counted
        await _call(limiter, "sendMessage", chat="-100123")

    asyncio.run(run())
    counter.flush()
    # A fresh instance (gateway restart) reads the same day back from disk.
    window = DailyCallCounter(tmp_path, wall=wall).window()
    dm = window["chats"]["2027045491"]
    assert dm["total"] == 4
    assert dm["endpoints"] == {"sendMessage": 1, "editMessageText": 2, "deleteMessage": 1}
    assert dm["triggers"] == {"goal": 3, "untagged": 1}
    assert window["chats"]["-100123"]["total"] == 1
    assert window["total"] == 5


def test_flush_is_additive_and_window_rolls_after_24h(tmp_path):
    wall = _Wall()
    a = DailyCallCounter(tmp_path, wall=wall)
    b = DailyCallCounter(tmp_path, wall=wall)  # e.g. the standalone lane sharing the profile
    a.record("1", "sendMessage", "typed")
    b.record("1", "sendMessage", "typed")
    a.flush()
    b.flush()
    a.flush()  # nothing pending: no double count
    assert a.window("1")["chats"]["1"]["total"] == 2
    wall.t += 25 * 3600
    assert a.window("1")["total"] == 0
    with sqlite3.connect(tmp_path / "telegram-flood-state.db") as conn:
        assert conn.execute("SELECT SUM(count) FROM call_counts").fetchone()[0] == 2


def test_local_refusals_and_shed_calls_are_not_counted(tmp_path):
    wall = _Wall()
    limiter, counter = _limiter(tmp_path, wall, penalty_remaining=lambda key: 30.0)

    async def run():
        with pytest.raises(Exception):
            await _call(limiter, "sendMessage")

    asyncio.run(run())
    assert counter.window()["total"] == 0

    limiter2, counter2 = _limiter(tmp_path / "b", wall)
    (tmp_path / "b").mkdir()
    limiter2.budget = ChatOutboundBudget(gap_override=10.0, clock=lambda: 0.0)

    async def run2():
        assert await _call(limiter2, "sendChatAction") == "ok"
        assert await _call(limiter2, "sendChatAction") is True  # shed: never reached Telegram

    asyncio.run(run2())
    assert counter2.window()["chats"]["2027045491"]["endpoints"] == {"sendChatAction": 1}


def test_long_retry_after_logs_the_window_counts(tmp_path, caplog):
    from telegram.error import RetryAfter
    wall = _Wall()
    recorded = []
    limiter, counter = _limiter(tmp_path, wall, on_retry_after=lambda key, wait: recorded.append((key, wait)))
    counter.record("2027045491", "editMessageText", "process")

    async def run():
        with pytest.raises(RetryAfter):
            await _call(limiter, "editMessageText", result=RetryAfter(dt.timedelta(seconds=34507)))

    with caplog.at_level(logging.INFO, logger=chat_budget.__name__):
        asyncio.run(run())
        _drain(counter)
    assert recorded == [("2027045491", 34507.0)]
    text = caplog.text
    assert "retry_after=34507.0s (trigger untagged)" in text
    assert "(retry_after=34507s on chat 2027045491): 2 total" in text


def test_long_retry_after_summary_covers_every_chat(tmp_path, caplog):
    """Per-chat vs per-bot: the refusal must log every chat's window, not just the offender's."""
    from telegram.error import RetryAfter
    wall = _Wall()
    limiter, counter = _limiter(tmp_path, wall)
    counter.record("-100777", "sendMessage", "untagged")
    counter.record("-100777", "sendMessage", "untagged")

    async def run():
        with pytest.raises(RetryAfter):
            await _call(limiter, "editMessageText", result=RetryAfter(dt.timedelta(seconds=3600)))

    with caplog.at_level(logging.INFO, logger=chat_budget.__name__):
        asyncio.run(run())
        _drain(counter)
    assert "Telegram chat -100777 calls since" in caplog.text
    assert "Telegram chat 2027045491 calls since" in caplog.text


def test_default_counter_is_bound_to_the_callers_profile(tmp_path, monkeypatch):
    """Two profiles in one process must not share (or cross-persist) a counter."""
    import hermes_constants
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: a)
    counter_a = chat_budget.call_counter()
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: b)
    counter_b = chat_budget.call_counter()
    assert counter_a is not counter_b
    assert counter_a._profile_dir == a and counter_b._profile_dir == b
    counter_b.record("1", "sendMessage", "typed")
    counter_b.flush()
    assert (b / "telegram-flood-state.db").exists() and not (a / "telegram-flood-state.db").exists()


def _drain(counter, timeout=5.0):
    """Wait for the background worker to finish its queued flush and summaries."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if counter.idle():
            return
        _time.sleep(0.01)
    raise AssertionError("counter worker did not drain")


def test_record_never_blocks_on_a_locked_database(tmp_path):
    """Regression: persistence ran inline in record(), so a locked DB stalled the send path ~1-2s."""
    import time as _time
    wall = _Wall()
    counter = DailyCallCounter(tmp_path, wall=wall, flush_interval=60.0)
    counter.record("1", "sendMessage", "typed")
    counter.flush()  # create the table
    blocker = sqlite3.connect(tmp_path / "telegram-flood-state.db", timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        wall.t += 61  # a flush is now due
        started = _time.monotonic()
        for _ in range(50):
            counter.record("1", "editMessageText", "goal")
        assert _time.monotonic() - started < 0.2
    finally:
        blocker.rollback()
        blocker.close()
    _drain(counter)
    counter.flush()
    assert counter.window("1")["chats"]["1"]["total"] == 51  # nothing lost while locked


def test_quiet_profile_persists_without_another_call(tmp_path):
    """Regression: a lone call stayed in memory until another call arrived after the interval."""
    import time as _time
    counter = DailyCallCounter(tmp_path, wall=_time.time, flush_interval=0.05, log_interval=3600.0)
    counter.record("1", "sendMessage", "typed")
    deadline = _time.monotonic() + 5
    while _time.monotonic() < deadline:
        db = tmp_path / "telegram-flood-state.db"
        if db.exists():
            try:
                with sqlite3.connect(db) as conn:
                    row = conn.execute("SELECT SUM(count) FROM call_counts").fetchone()
            except sqlite3.OperationalError:  # file created, table not committed yet
                row = None
            if row and row[0] == 1:
                return
        _time.sleep(0.02)
    raise AssertionError("count was never persisted without a second call")


def test_window_never_reaches_back_before_its_cutoff(tmp_path):
    """Regression: flooring the cutoff to the hour made a 24h window cover up to 25h."""
    hour0 = 1_791_200_000.0 - (1_791_200_000.0 % 3600)
    wall = _Wall(hour0 + 1800)  # half past an hour
    counter = DailyCallCounter(tmp_path, wall=wall)
    old_hour = hour0 - 86400  # the bucket that holds the 24h cutoff (cutoff = old_hour + 1800)
    counter.record("1", "sendMessage", "typed")
    counter._dirty[(old_hour, "1", "sendMessage", "goal")] = 7
    window = counter.window("1")
    assert window["chats"]["1"]["total"] == 1
    assert window["since"] == old_hour + 3600 and window["since"] >= wall.t - 86400


def test_queued_turn_is_labelled_by_its_own_event(monkeypatch):
    """Regression: a busy-session event drained later ran under its predecessor's trigger."""
    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter
    from plugins.platforms.telegram.adapter import TelegramAdapter

    seen = []

    async def fake_process(self, event, session_key):
        seen.append(("task", current_trigger()))
        # The runner then drains the parked follow-up in-band on this same task.
        self._pending_messages[session_key] = SimpleNamespace(
            text="[ASYNC DELEGATION BATCH COMPLETE — deleg_1]", internal=True)
        self.get_pending_message(session_key)
        seen.append(("inband", current_trigger()))

    monkeypatch.setattr(BasePlatformAdapter, "_process_message_background", fake_process)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    typed = bind_trigger(SimpleNamespace(text="hello", internal=False))  # the draining context
    try:
        goal = SimpleNamespace(text="[Continuing toward your standing goal] Goal: x", internal=True)
        asyncio.run(adapter._process_message_background(goal, "k"))
        assert current_trigger() == "typed"  # restored for the caller
    finally:
        reset_trigger(typed)
    assert seen == [("task", "goal"), ("inband", "delegation")]


def test_window_counts_what_a_locked_database_could_not_store(tmp_path):
    wall = _Wall()
    counter = DailyCallCounter(tmp_path, wall=wall)
    counter.record("1", "sendMessage", "typed")
    counter.flush()
    counter.record("1", "editMessageText", "goal")
    blocker = sqlite3.connect(tmp_path / "telegram-flood-state.db", timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert counter.window("1")["chats"]["1"]["triggers"] == {"goal": 1}  # DB unreadable: dirty only
    finally:
        blocker.rollback()
        blocker.close()
    window = counter.window("1")
    assert window["chats"]["1"]["total"] == 2 and window["chats"]["1"]["triggers"] == {"typed": 1, "goal": 1}


def test_counter_failure_never_blocks_a_send(tmp_path):
    class Broken:
        def record(self, *a, **k):
            raise OSError("disk full")

    limiter = ChatBudgetRateLimiter(ChatOutboundBudget(gap_override=0.0), counter=Broken())
    assert asyncio.run(_call(limiter, "sendMessage")) == "ok"


def test_hourly_summary_is_logged(tmp_path, caplog):
    wall = _Wall()
    counter = DailyCallCounter(tmp_path, wall=wall, flush_interval=60.0, log_interval=3600.0)
    with caplog.at_level(logging.INFO, logger=chat_budget.__name__):
        counter.record("1", "sendMessage", "typed")
        assert "calls since" not in caplog.text
        wall.t += 3601
        counter.record("1", "editMessageText", "goal")
        _drain(counter)
    assert "Telegram chat 1 calls since" in caplog.text and "(hourly): 2 total" in caplog.text
