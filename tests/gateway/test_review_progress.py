import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


@pytest.mark.asyncio
async def test_native_review_child_progress_uses_existing_delegation_progress_path():
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    cards = SimpleNamespace(observe=AsyncMock())
    runner = SimpleNamespace(_delegation_cards=cards)
    ctx = TurnContext(source=source, session_key="route", session_id="parent", run_generation=1,
        tool_progress_enabled=True, progress_mode="all", _run_still_current=lambda: True)
    relay = TurnRunner(runner, ctx)  # type: ignore[arg-type]
    tasks = []
    relay._schedule = lambda coro, *_args, **_kwargs: tasks.append(asyncio.create_task(coro))  # type: ignore[method-assign]

    relay.progress_callback("subagent.start", delegation_id="review-1", native_review=True,
                            parent_task_id="a" * 32, thread_ref="A")
    await asyncio.gather(*tasks)

    cards.observe.assert_awaited_once()
    assert cards.observe.call_args.args[4] == "subagent.start"
