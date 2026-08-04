"""Real-code check: concurrent sessions in ONE chat share the edit throttle.

The unit tests in tests/test_progress_edit_chat_throttle.py pin the gate
arithmetic against a mirror of the logic. This module drives the ACTUAL
``send_progress_messages`` consumer in gateway/run.py so the wiring itself
is covered — the shared clock, the key, and the up-front slot claim.

Telegram rate-limits per chat, but the progress consumer runs per session
and Telegram DM topics all share one chat_id. Before the fix each session
throttled on its own clock, so N concurrent sessions produced ~N times the
intended edit rate against one chat and tripped flood control.
"""
import asyncio
import importlib
import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource

from tests.gateway.test_run_progress_interrupt import (  # noqa: E402
    ProgressCaptureAdapter,
    _make_runner,
)


_PROGRESS_EDIT_INTERVAL = 1.5  # must match gateway/run.py
_RUN_SECONDS = 4.0
_SESSIONS = 4


class _EditCountingAdapter(ProgressCaptureAdapter):
    """Records the wall-clock time of every edit against the chat."""

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform)
        self.edit_times = []

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edit_times.append(time.monotonic())
        return await super().edit_message(chat_id, message_id, content)


class _ChattyProgressAgent:
    """Emits tool-progress continuously so the consumer is always ready
    to edit — the throttle, not the event supply, must be what limits it."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []
        self._interrupt_requested = False

    @property
    def is_interrupted(self) -> bool:
        return self._interrupt_requested

    def run_conversation(self, message, conversation_history=None, task_id=None):
        deadline = time.monotonic() + _RUN_SECONDS
        i = 0
        while time.monotonic() < deadline:
            i += 1
            # Vary the text so the consumer's dedup can't collapse events.
            self.tool_progress_callback(
                "tool.started", "web_search", f"query {i}", {},
            )
            time.sleep(0.05)
        return {"final_response": "done", "messages": [], "api_calls": 1}


@pytest.mark.asyncio
async def test_concurrent_sessions_in_one_chat_share_the_edit_budget(
    monkeypatch, tmp_path
):
    """N sessions in one chat must not multiply the chat's edit rate by N."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _ChattyProgressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = _EditCountingAdapter()
    runner = _make_runner(adapter)
    # _make_runner uses object.__new__, so GatewayRunner.__init__ never ran
    # and the shared clock attribute is absent. Supply it explicitly — the
    # production runner creates this in __init__.
    runner._progress_edit_clock = {}

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )

    # Same chat_id, different thread_ids — exactly the Telegram DM-topic shape.
    async def _one(thread_id: str):
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id=thread_id,
        )
        return await runner._run_agent(
            message="hi",
            context_prompt="",
            history=[],
            source=source,
            session_id=f"sess-{thread_id}",
            session_key=f"agent:main:telegram:group:-1001:{thread_id}",
        )

    started = time.monotonic()
    results = await asyncio.gather(
        *[_one(f"thread-{i}") for i in range(_SESSIONS)]
    )
    elapsed = time.monotonic() - started

    assert all(r["final_response"] == "done" for r in results)

    # Budget for ONE chat: elapsed / interval, plus slack for the initial
    # bubble per session and scheduling jitter. Without the shared clock the
    # ceiling would be _SESSIONS times higher.
    budget = (elapsed / _PROGRESS_EDIT_INTERVAL) + _SESSIONS + 2
    unshared_ceiling = (elapsed / _PROGRESS_EDIT_INTERVAL) * _SESSIONS

    assert len(adapter.edit_times) <= budget, (
        f"{len(adapter.edit_times)} edits against one chat in {elapsed:.1f}s "
        f"(budget {budget:.1f}). The per-session throttles are not sharing a "
        f"clock, so concurrent sessions multiply the chat's edit rate and "
        f"trip Telegram flood control."
    )
    # Guard the test itself: if this fires, the workload was too light to
    # distinguish shared from unshared and the assertion above proved nothing.
    assert unshared_ceiling > budget, (
        "test workload too small to discriminate shared vs per-session "
        "throttling — raise _RUN_SECONDS or _SESSIONS"
    )


@pytest.mark.asyncio
async def test_shared_clock_is_keyed_by_platform_and_chat(monkeypatch, tmp_path):
    """The clock key must be per platform+chat, not global."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _ChattyProgressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = _EditCountingAdapter()
    runner = _make_runner(adapter)
    runner._progress_edit_clock = {}

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-4242",
        chat_type="group",
        thread_id="t1",
    )
    await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-key",
        session_key="agent:main:telegram:group:-4242:t1",
    )

    assert runner._progress_edit_clock, "no throttle slot was ever claimed"
    assert "telegram:-4242" in runner._progress_edit_clock, (
        f"expected a 'telegram:-4242' key, got "
        f"{list(runner._progress_edit_clock)} — a mis-keyed clock either "
        f"throttles unrelated chats together or not at all"
    )
