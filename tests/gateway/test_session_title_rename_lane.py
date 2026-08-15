"""Which title stage is allowed to spend a platform rename.

Titling is two-stage: a derived slice of the user's own words lands inline, and
the model's version replaces it a moment later. A local sidebar wants both. A
Discord thread or a Telegram topic wants only the second — renaming twice lands
on the same name at twice the cost, and Discord allows two channel renames per
ten minutes, so the throwaway can be the one that survives.
"""

from __future__ import annotations

import types

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner, TurnRunner
from gateway.session import SessionSource


def _attach(lane):
    """Attach title handling for *lane* and return (agent, renames)."""
    renames: list = []
    source = types.SimpleNamespace(platform=Platform.DISCORD, chat_id="chan-1")

    runner = types.SimpleNamespace(
        _is_telegram_topic_lane=lambda src: lane == "telegram",
        _is_discord_auto_thread_lane=lambda src: lane == "discord",
        _is_relay_discord_channel_lane=lambda src: False,
        _schedule_telegram_topic_title_rename=(
            lambda src, sid, title, **kwargs: renames.append(title)
        ),
        _schedule_discord_semantic_thread_rename=(
            lambda src, sid, title: renames.append(title)
        ),
    )
    holder = types.SimpleNamespace(
        _runner=runner,
        _attach_session_title_callback=TurnRunner._attach_session_title_callback,
    )
    agent = types.SimpleNamespace(session_id="sess-1")
    holder._attach_session_title_callback(
        holder, agent, types.SimpleNamespace(source=source)
    )
    return agent, renames


def test_discord_rename_waits_for_the_model_title():
    agent, renames = _attach("discord")
    callback = agent._on_session_title

    callback("fix the flaky auth test in log", "derived")
    assert renames == []

    callback("Fix flaky auth test", "llm")
    assert renames == ["Fix flaky auth test"]


@pytest.mark.asyncio
async def test_native_thread_rename_passes_only_the_initial_name_guard():
    """The shared rename lane must honor the strict native adapter contract."""
    calls: list[tuple[str, str, str | None]] = []

    class StrictNativeAdapter:
        async def rename_thread(
            self,
            thread_id: str,
            name: str,
            *,
            only_if_current_name: str | None = None,
        ) -> bool:
            calls.append((thread_id, name, only_if_current_name))
            return True

    class NativeRenameRunner:
        _is_discord_auto_thread_lane = GatewayRunner._is_discord_auto_thread_lane
        _sanitize_discord_thread_title = GatewayRunner._sanitize_discord_thread_title
        _rename_discord_auto_thread_for_session_title = (
            GatewayRunner._rename_discord_auto_thread_for_session_title
        )

        def __init__(self, adapter):
            self.adapters = {Platform.DISCORD: adapter}

        def _adapter_for_source(self, source):
            return self.adapters[source.platform]

    source = types.SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        thread_id="999",
        auto_thread_created=True,
        auto_thread_initial_name="Initial words",
    )

    runner = NativeRenameRunner(StrictNativeAdapter())
    await runner._rename_discord_auto_thread_for_session_title(
        source,
        "session-1",
        "Semantic Session Title",
    )

    assert calls == [("999", "Semantic Session Title", "Initial words")]


def test_telegram_topic_title_is_deferred_until_response():
    agent, renames = _attach("telegram")

    assert agent._defer_topic_title_until_response is True
    assert not hasattr(agent, "_on_session_title")
    assert renames == []


@pytest.mark.asyncio
async def test_telegram_topic_skips_placeholder_rename(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._telegram_topic_last_scheduled_titles = {}
    runner._is_telegram_topic_lane = lambda source: True
    runner._telegram_topic_auto_rename_disabled = lambda source: False
    runner._sanitize_telegram_topic_title = lambda title: title.strip()
    runner._gateway_loop = None
    scheduled = []

    def capture(coro, loop, **kwargs):
        coro.close()
        scheduled.append(True)
        return None

    monkeypatch.setattr("gateway.run.safe_schedule_threadsafe", capture)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
    )

    runner._schedule_telegram_topic_title_rename(source, "session-1", "User request:")

    assert scheduled == []


@pytest.mark.asyncio
async def test_telegram_topic_deduplicates_same_title_request(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._telegram_topic_last_scheduled_titles = {}
    runner._is_telegram_topic_lane = lambda source: True
    runner._telegram_topic_auto_rename_disabled = lambda source: False
    runner._sanitize_telegram_topic_title = lambda title: title.strip()
    runner._gateway_loop = None
    scheduled = []

    def capture(coro, loop, **kwargs):
        coro.close()
        scheduled.append(True)
        return None

    monkeypatch.setattr("gateway.run.safe_schedule_threadsafe", capture)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
    )

    runner._schedule_telegram_topic_title_rename(source, "session-1", "Real title")
    runner._schedule_telegram_topic_title_rename(source, "session-1", "Real title")

    assert scheduled == [True]
