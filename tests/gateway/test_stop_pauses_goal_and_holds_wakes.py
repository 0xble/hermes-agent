"""Invariant: a gateway ``/stop`` stays stopped until the user sends something.

Before this, ``/stop`` killed the turn and its background delegations, but (1) the standing
``/goal`` stayed active and (2) the delegations' own "interrupted" completions were injected as a
fresh turn one second later. That turn's post-turn judge queued goal continuations, so the agent
kept talking after the user stopped it. The CLI already paused the goal on Ctrl+C.

Real ``GatewayRunner`` + real ``BasePlatformAdapter`` subclass, real goal SQLite state, real
``tools.async_delegation`` durable rows; no Telegram or LLM traffic.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import tools.async_delegation as ad
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.TELEGRAM)
        self.sent: list[str] = []

    @property
    def name(self):
        return "telegram"

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()  # warm off the loop thread (see test_goal_verdict_send)
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()
    goals._DB_CACHE.clear()


async def _runner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "u1")
    adapter = _Adapter()
    runner = GatewayRunner(config=GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="x")}))
    runner.adapters = {Platform.TELEGRAM: adapter}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="u1", user_name="tester")
    key = adapter._event_session_key(MessageEvent(text="", message_type=MessageType.TEXT, source=source))
    # Route through the real store so the in-memory index and state.db contain the same
    # session.  A hand-inserted SessionEntry leaves the durable row absent, and the
    # production stale-route guard then self-heals it to a different session during /stop.
    entry = await runner.async_session_store.get_or_create_session(source)
    session_id = entry.session_id
    return runner, adapter, source, key, session_id


def _active_goal(session_id: str):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id)
    mgr.set("migrate the persistent agent stack")
    return mgr


def _blocked_goal(session_id: str):
    mgr = _active_goal(session_id)
    with patch("hermes_cli.goals.judge_goal",
               return_value=("blocked", "needs the user to answer", False, None, False)):
        assert mgr.evaluate_after_turn("May I proceed?")["status"] == "paused"
    return mgr


def _interrupted_completion(key: str, delegation_id: str) -> dict:
    evt = {"type": "async_delegation", "session_key": key, "delegation_id": delegation_id,
           "summary": None, "error": "interrupted", "status": "interrupted", "dispatched_at": time.time(),
           "platform": "telegram", "chat_type": "dm", "chat_id": "c1", "user_id": "u1"}
    ad._persist_dispatch(evt)
    ad._persist_completion(evt, {"status": "interrupted", "summary": None})
    return evt


@pytest.mark.asyncio
@pytest.mark.parametrize("session_state", ["idle", "busy"])
@pytest.mark.parametrize("goal_state", ["active", "judge-blocked"])
async def test_stop_pauses_goal_so_user_input_does_not_revive_it(monkeypatch, session_state, goal_state):
    from hermes_cli.goals import GoalManager

    runner, adapter, source, key, session_id = await _runner(monkeypatch)
    (_active_goal if goal_state == "active" else _blocked_goal)(session_id)
    continuation = runner._synthetic_prompt_event(source, "[Continuing toward your standing goal]\nGoal: x")
    adapter._pending_messages[key] = continuation

    if session_state == "busy":
        await runner._interrupt_and_clear_session(key, source, interrupt_reason="Stop requested",
                                                  invalidation_reason="stop_command")
    else:
        reply = await runner._handle_stop_command(MessageEvent(text="/stop", message_type=MessageType.TEXT,
                                                               source=source))
        assert "Stopped" in str(reply)

    state = GoalManager(session_id).state
    assert state.status == "paused"
    assert state.paused_reason == "user-interrupted (/stop)"
    assert key not in adapter._pending_messages  # queued continuation dropped
    assert any(m.startswith("⏸ Goal paused") for m in adapter.sent)
    # An explicit stop is not a judge BLOCK: ordinary user input must not revive it.
    assert GoalManager(session_id).resume_for_user_input() is False


@pytest.mark.asyncio
async def test_completions_from_a_stop_are_held_until_the_user_sends_a_turn(monkeypatch):
    runner, adapter, source, key, _session_id = await _runner(monkeypatch)
    received: list[MessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)
    await runner._interrupt_and_clear_session(key, source, interrupt_reason="Stop requested",
                                              invalidation_reason="stop_command")
    group = [_interrupted_completion(key, f"deleg_stop_{i}") for i in range(2)]

    for _ in range(3):  # the watcher retries every tick; holding must never spend an attempt
        assert await runner._deliver_async_delegation_group(group) is False
    assert not received
    for evt in group:
        row = ad.get_durable_delegation(evt["delegation_id"])
        assert (row["delivery_state"], row["delivery_attempts"]) == ("pending", 0)

    # The user's next admitted turn releases the hold; internal wakes never do.
    assert runner._is_user_turn_event(MessageEvent(text="internal", source=source, internal=True)) is False
    assert runner._is_user_turn_event(MessageEvent(text="continue", source=source)) is True
    runner._clear_user_stop_latch(key)
    assert await runner._deliver_async_delegation_group(group) is True
    for task in list(adapter._background_tasks):
        await task
    assert len(received) == 1 and received[0].internal
