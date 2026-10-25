from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE
from tests.gateway.test_goal_continuation_drain import hermes_home as hermes_home


class FakeAdapter:
    def __init__(self, results=None):
        self.calls = []
        self.callbacks = {}
        self.callback_registrations = []
        self._active_sessions = {}
        self.results = list(results or [])

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return self.results.pop(0) if self.results else SimpleNamespace(success=True)

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self.callback_registrations.append((session_key, generation, callback))
        self.callbacks[session_key] = (generation, callback)


def _goal_continuation_event(source, goal="finish the task"):
    return MessageEvent(
        text=CONTINUATION_PROMPT_TEMPLATE.format(goal=goal),
        message_type=MessageType.TEXT,
        source=source,
    )


@pytest.mark.asyncio
async def test_goal_status_notice_defers_until_post_delivery_callback():
    """Regression: goal status must appear after the agent's visible reply.

    _post_turn_goal_continuation runs before BasePlatformAdapter sends the
    returned final response. It should therefore register a post-delivery
    callback, not send the judge status immediately.
    """
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="parent-channel",
        thread_id="thread-123",
        user_id="user-1",
    )

    await runner._defer_goal_status_notice_after_delivery(source, "✓ Goal achieved: done")

    assert adapter.calls == []
    assert len(adapter.callbacks) == 1

    _, callback = next(iter(adapter.callbacks.values()))
    result = callback()
    if hasattr(result, "__await__"):
        await result

    assert adapter.calls == [
        {
            "chat_id": "parent-channel",
            "content": "✓ Goal achieved: done",
            "reply_to": None,
            "metadata": {"thread_id": "thread-123"},
        }
    ]


@pytest.mark.asyncio
async def test_goal_status_notice_retries_short_flood_in_background(monkeypatch):
    from gateway import run_goals

    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter([
        SimpleNamespace(success=False, error="flood_control:3.0"),
        SimpleNamespace(success=True),
    ])
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner._background_tasks = set()

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="parent-channel",
        thread_id="thread-123",
        user_id="user-1",
    )
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(run_goals.asyncio, "sleep", fake_sleep)

    await runner._send_goal_status_notice(source, "↻ Continuing toward goal: more work")

    assert len(adapter.calls) == 1
    task = next(iter(runner._background_tasks))
    await task
    assert len(adapter.calls) == 2
    assert 3.0 < sleeps[0] < 4.0


@pytest.mark.asyncio
async def test_goal_status_notice_logs_once_when_flood_wait_is_too_long(caplog):
    from gateway import run_goals

    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter([SimpleNamespace(success=False, error="flood_control:31.0")])
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner._background_tasks = set()
    source = SessionSource(platform=Platform.DISCORD, chat_id="parent-channel", user_id="user-1")

    with caplog.at_level("WARNING", logger="gateway.run"):
        await runner._send_goal_status_notice(source, "⏸ Goal blocked — paused: needs input")

    assert len(adapter.calls) == 1
    assert "notice_kind=blocked" in caplog.text
    assert "flood_control:31.0" in caplog.text


@pytest.mark.asyncio
async def test_goal_status_notice_bounds_flood_retries_and_warns_once(monkeypatch, caplog):
    from gateway import run_goals

    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter([
        SimpleNamespace(success=False, error="flood_control:6.0"),
        SimpleNamespace(success=False, error="flood_control:6.0"),
        SimpleNamespace(success=False, error="flood_control:6.0"),
    ])
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner._background_tasks = set()

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(run_goals.asyncio, "sleep", fake_sleep)
    source = SessionSource(platform=Platform.DISCORD, chat_id="parent-channel", user_id="user-1")

    with caplog.at_level("WARNING", logger="gateway.run"):
        await runner._send_goal_status_notice(source, "✓ Goal achieved: blocked issue resolved")
        await next(iter(runner._background_tasks))

    assert len(adapter.calls) == 3
    assert caplog.text.count("goal continuation: status send failed") == 1
    assert "notice_kind=achieved" in caplog.text


@pytest.mark.asyncio
async def test_goal_status_notice_logs_when_retry_is_cancelled(monkeypatch, caplog):
    from gateway import run_goals

    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter([SimpleNamespace(success=False, error="flood_control:3.0")])
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner._background_tasks = set()
    source = SessionSource(platform=Platform.DISCORD, chat_id="parent-channel", user_id="user-1")
    retry_started = asyncio.Event()
    retry_wait = asyncio.Event()

    async def wait_for_retry(_delay):
        retry_started.set()
        await retry_wait.wait()

    monkeypatch.setattr(run_goals.asyncio, "sleep", wait_for_retry)
    with caplog.at_level("WARNING", logger="gateway.run"):
        await runner._send_goal_status_notice(source, "↻ Continuing toward goal: more work")
        await retry_started.wait()
        task = next(iter(runner._background_tasks))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "goal continuation: status retry cancelled" in caplog.text
    assert "notice_kind=continuing" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("message,kind,protected", [
    ("✓ Goal achieved: verified", None, True),
    ("⏸ Goal blocked — paused: needs input", None, True),
    ("⏸ Goal paused — judge API returned errors", None, True),
    ("⏸ Goal paused — quality gate exhausted retries", None, True),
    ("⏸ Goal paused — blocker: exceeded the 6h lifetime ceiling", "wait-age", True),
    ("✗ Quality gate failed (3/20 turns, attempt 1/3): $ check", None, True),
    # Terminal silent-stall stop: the goal is already cleared, so shedding it loses the outcome.
    ("✗ Goal stopped: 3 silent rechecks returned continue with no new evidence.", None, True),
    ("▶ Goal resumed: ship it", "resumed", True),
    ("⏳ Goal parked: waiting for review", None, False),
    ("↻ Continuing toward goal: more work", None, False),
    ("⏳ Goal still waiting on pid 42 after 30 minutes", "wait-age", False),
    ("Goal status: unchanged", None, False),
])
async def test_goal_notice_real_daily_quota_pressure(tmp_path, message, kind, protected):
    """Real DailyQuota + Telegram send path; only the network transport is mocked."""
    from tests.gateway.test_telegram_daily_quota import CHAT, _adapter, _spend

    transport = _adapter(tmp_path)
    _spend(transport, 11)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner._background_tasks = set()
    runner._goal_notice_adapter = lambda source: transport
    runner._thread_metadata_for_source = lambda source: {"thread_id": "213"}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT, chat_type="dm", thread_id="213")

    await runner._send_goal_status_notice(source, message, notice_kind=kind)
    assert transport._bot.send_message.await_count == int(protected), message
    assert transport._daily_quota().messages(CHAT) == 11  # mocked Bot API does not enter the limiter


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["✓ Goal achieved: verified", "⏸ Goal blocked — paused: needs input"])
async def test_deferred_outcome_keeps_protection_through_flood_retry(tmp_path, message):
    from telegram.error import RetryAfter
    from tests.gateway.test_telegram_daily_quota import CHAT, _adapter, _spend

    transport = _adapter(tmp_path)
    _spend(transport, 11)
    transport._bot.send_message.side_effect = [RetryAfter(1), SimpleNamespace(message_id=222)]
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner._background_tasks = set()
    runner._goal_notice_adapter = lambda source: transport
    runner._thread_metadata_for_source = lambda source: {"thread_id": "213"}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT, chat_type="dm", thread_id="213")
    callbacks = []
    transport._active_sessions = {}
    transport.register_post_delivery_callback = lambda key, callback, **kwargs: callbacks.append(callback)
    await runner._defer_goal_status_notice_after_delivery(source, message)
    assert transport._bot.send_message.await_count == 0
    assert len(callbacks) == 1
    await callbacks.pop()()
    await asyncio.gather(*list(runner._background_tasks))
    await asyncio.sleep(0)
    assert transport._bot.send_message.await_count == 2
    assert transport._daily_quota().messages(CHAT) == 11
    assert not callbacks
    assert not runner._background_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,status", [("done", "done"), ("blocked", "paused")])
async def test_real_post_turn_outcome_survives_daily_quota(hermes_home, tmp_path, monkeypatch, verdict, status):
    """Real GoalManager CAS -> gateway deferred callback -> real quota/adapter send."""
    from gateway.platforms.base import OUTBOUND_FINAL, current_outbound_class
    from hermes_cli import goals
    from tests.gateway.test_goal_continuation_notice_dedupe import _harness
    from tests.gateway.test_telegram_daily_quota import CHAT, _adapter, _spend

    runner, _, entry, _, _ = _harness(hermes_home)
    transport = _adapter(tmp_path)
    _spend(transport, 11)
    seen = []
    real_send = transport.send

    async def send(*args, **kwargs):
        seen.append(current_outbound_class())
        return await real_send(*args, **kwargs)

    monkeypatch.setattr(transport, "send", send)
    runner._goal_notice_adapter = lambda source: transport
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT, thread_id="213", chat_type="dm")
    monkeypatch.setattr(goals, "judge_goal", lambda *args, **kwargs: (verdict, "a material outcome", False, None, False))
    await runner._post_turn_goal_continuation(session_entry=entry, source=source, final_response="A material result.")
    assert goals.GoalManager(entry.session_id).state.status == status
    assert transport._bot.send_message.await_count == 0
    callback = transport.pop_post_delivery_callback(runner._session_key_for_source(source))
    assert callback is not None
    await callback()
    assert seen == [OUTBOUND_FINAL]
    assert transport._bot.send_message.await_count == 1
    assert transport._bot.send_message.await_args.kwargs["message_thread_id"] == 213

