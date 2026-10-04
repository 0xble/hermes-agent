from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE


class FakeAdapter:
    def __init__(self, results=None):
        self.calls = []
        self.callbacks = {}
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
        await runner._send_goal_status_notice(source, "✓ Goal achieved: done")
        await next(iter(runner._background_tasks))

    assert len(adapter.calls) == 3
    assert caplog.text.count("goal continuation: status send failed") == 1
    assert "notice_kind=achieved" in caplog.text


