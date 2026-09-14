"""Runtime producer → child relay → gateway → durable display, without network."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.delegation_cards import DelegationCards, render_card
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from tools.delegate_tool_progress import _ChildProgressRelay


@pytest.mark.asyncio
async def test_runtime_wait_priority_sequence_privacy_and_attempt_clearing(tmp_path):
    from agent.delegation_activity import observed_wait

    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    cards = runner._delegation_cards = DelegationCards(runner, home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    owner = dict(profile="default", session_id="parent", session_key="route", chat_id="42", thread_id="")
    identity = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Check runtime", owner=owner,
                    attempt=0, session_id="child")
    ctx = TurnContext(source=source, session_id="parent", session_key="route", run_generation=1,
                      tool_progress_enabled=True, progress_mode="all", _run_still_current=lambda: False)
    gateway = TurnRunner(runner, ctx)
    tasks, events = [], []
    gateway._schedule = lambda coro, *_: tasks.append(asyncio.create_task(coro))
    def deliver(*args, **kwargs):
        events.append((args, copy.deepcopy(kwargs)))
        gateway.progress_callback(*args, **kwargs)
    relay = _ChildProgressRelay(0, "PRIVATE goal", None, deliver, 1, "sa", None, 0, "model", [], identity)
    async def drain():
        await asyncio.gather(*tasks)
        while cards.pending:
            await asyncio.gather(*list(cards.pending.values()))
    def text():
        return render_card(cards.cards[identity["parent_task_id"]])
    relay("subagent.start")
    relay("tool.started", "terminal", "PRIVATE args", {"secret": "PRIVATE"})
    await drain()
    assert "terminal" in text()
    with observed_wait(relay, "process"):
        await drain()
        assert "Waiting for process" in text()
        stale = events[-1]
        relay("tool.started", "read_file", "PRIVATE", {"secret": "PRIVATE"})
        relay("_thinking", "PRIVATE")
        await drain()
        assert "Waiting for process" in text()  # housekeeping does not erase the real wait
        with observed_wait(relay, "provider_rate_limit"):
            await drain()
            assert "Provider rate limit" in text()
        await drain()
        assert "Waiting for process" in text()
    await drain()
    assert "read_file" in text() and "Waiting" not in text()
    gateway.progress_callback(*stale[0], **stale[1])
    await drain()
    assert "Waiting" not in text()  # late sequence cannot reinstall an ended wait
    relay("runtime.wait", reason="PRIVATE unknown", wait_id="PRIVATE", active=True)
    relay("runtime.wait", reason=["PRIVATE"], wait_id="PRIVATE", active=True)
    await drain()
    assert "PRIVATE" not in cards.path.read_text()
    with observed_wait(relay, "process"):
        relay("subagent.complete", status="failed")
    await drain()
    assert text() == "! Check runtime"
    await cards.observe(source, "route", "parent", 2, "subagent.admitted", None,
        {**identity, "child_session_id": "child", "attempt": 1, "resume_claim_id": "resume"})
    gateway.progress_callback(*stale[0], **stale[1])
    await drain()
    row = cards.cards[identity["parent_task_id"]]["rows"]["A"]
    assert row["attempt"] == 1 and "activity_reason" not in row and row["last_tool"] is None
    assert text() == "○ Check runtime"
    resumed = _ChildProgressRelay(0, "PRIVATE", None, deliver, 1, "sa", None, 0, "model", [],
        {**identity, "attempt": 1, "resume_claim_id": "resume"})
    resumed("tool.started", "web_search", activity_sequence=999, attempt=0)
    await drain()
    assert row["attempt"] == 1 and row["activity_sequence"] == 1
    assert "web_search" in text()  # runtime identity overrides event kwargs
    with observed_wait(resumed, "process"):
        await drain()
        restored = DelegationCards(runner, home=tmp_path, interval=0)
        recovered = restored.cards[identity["parent_task_id"]]["rows"]["A"]
        assert recovered["state"] == "unknown" and "activity_reason" not in recovered
        await restored.shutdown()
    await drain()
    resumed("subagent.complete", status="failed", terminal_reason="provider_billing")
    await drain()
    assert "Provider billing limit" in text()
    await cards.shutdown()


def test_child_prompt_uses_actual_depth_budget():
    from tools.delegate_tool_progress import _build_child_system_prompt
    from agent.delegation_labels import task_label_limit_for_depth
    for depth in (1, 2, 8):
        prompt = _build_child_system_prompt("Inspect", role="orchestrator", child_depth=depth, max_spawn_depth=10)
        assert f"at most {task_label_limit_for_depth(depth)} code points" in prompt


@pytest.mark.asyncio
async def test_real_process_wait_reaches_card_before_exit(tmp_path):
    from tools.process_registry import ProcessRegistry
    from tools.delegate_tool_registry import _active_subagents
    from tools.registry import registry
    from unittest.mock import patch
    import json

    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    cards = runner._delegation_cards = DelegationCards(runner, home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    owner = dict(profile="default", session_id="parent", session_key="route", chat_id="42", thread_id="")
    identity = dict(parent_task_id="c" * 32, thread_ref="A", task_label="Check process", owner=owner, attempt=0)
    ctx = TurnContext(source=source, session_id="parent", session_key="route", run_generation=1,
                      tool_progress_enabled=True, progress_mode="all", _run_still_current=lambda: False)
    gateway, loop, scheduled = TurnRunner(runner, ctx), asyncio.get_running_loop(), []
    gateway._schedule = lambda coro, *_: scheduled.append(asyncio.run_coroutine_threadsafe(coro, loop))
    entered = asyncio.Event()
    def deliver(kind, *args, **kw):
        gateway.progress_callback(kind, *args, **kw)
        if kind == "subagent.activity" and kw["activity_reason"] == "process":
            loop.call_soon_threadsafe(entered.set)
    relay = _ChildProgressRelay(0, "PRIVATE", None, deliver, 1, "proc-child", None, 0, None, [], identity)
    relay("subagent.start")
    relay("tool.started", "process_manage")
    local = ProcessRegistry()
    session = local.spawn_local("read line", cwd=str(tmp_path), task_id="proc-child", use_pty=True)
    try:
        with patch("tools.process_registry.process_registry", local), patch.dict(_active_subagents,
                {"proc-child": {"agent": SimpleNamespace(tool_progress_callback=relay)}}):
            work = asyncio.create_task(asyncio.to_thread(registry.dispatch, "process_manage",
                {"action": "wait", "session_id": session.id, "timeout": 10}, task_id="proc-child"))
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.gather(*(asyncio.wrap_future(f) for f in scheduled))
            assert "Waiting for process" in render_card(cards.cards[identity["parent_task_id"]])
            local.submit_stdin(session.id, "done")
            result = json.loads(await asyncio.wait_for(work, 10))
            assert result["status"] == "exited"
            await asyncio.gather(*(asyncio.wrap_future(f) for f in scheduled))
            text = render_card(cards.cards[identity["parent_task_id"]])
            assert "process_manage" in text and "Waiting for process" not in text
    finally:
        local.kill_process(session.id)
        await cards.shutdown()
