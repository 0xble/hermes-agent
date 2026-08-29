import asyncio

import pytest

from agent.reasoning_context import (
    begin_turn_reasoning,
    get_turn_reasoning_config,
    reset_turn_reasoning,
    set_turn_reasoning_config,
)


@pytest.mark.asyncio
async def test_turn_reasoning_isolated_between_concurrent_tasks():
    owner = object()
    both_ready = asyncio.Event()
    release = asyncio.Event()
    ready_count = 0
    ready_lock = asyncio.Lock()

    async def worker(effort: str) -> dict | None:
        nonlocal ready_count
        token = begin_turn_reasoning(owner)
        try:
            assert set_turn_reasoning_config(
                owner, {"enabled": True, "effort": effort}
            )
            async with ready_lock:
                ready_count += 1
                if ready_count == 2:
                    both_ready.set()
            await release.wait()
            return get_turn_reasoning_config(owner)
        finally:
            reset_turn_reasoning(token)

    low_task = asyncio.create_task(worker("low"))
    high_task = asyncio.create_task(worker("high"))
    await both_ready.wait()
    release.set()

    low, high = await asyncio.gather(low_task, high_task)

    assert low == {"enabled": True, "effort": "low"}
    assert high == {"enabled": True, "effort": "high"}
    assert get_turn_reasoning_config(owner) is None


def test_turn_reasoning_is_owned_and_nested_scope_restores():
    first_owner = object()
    second_owner = object()
    outer = begin_turn_reasoning(first_owner)
    try:
        assert set_turn_reasoning_config(
            first_owner, {"enabled": True, "effort": "low"}
        )
        assert get_turn_reasoning_config(first_owner) == {
            "enabled": True,
            "effort": "low",
        }
        assert get_turn_reasoning_config(second_owner) is None
        assert not set_turn_reasoning_config(
            second_owner, {"enabled": True, "effort": "high"}
        )

        inner = begin_turn_reasoning(second_owner)
        try:
            assert set_turn_reasoning_config(
                second_owner, {"enabled": True, "effort": "high"}
            )
            assert get_turn_reasoning_config(first_owner) is None
            assert get_turn_reasoning_config(second_owner) == {
                "enabled": True,
                "effort": "high",
            }
        finally:
            reset_turn_reasoning(inner)

        assert get_turn_reasoning_config(first_owner) == {
            "enabled": True,
            "effort": "low",
        }
    finally:
        reset_turn_reasoning(outer)

    assert get_turn_reasoning_config(first_owner) is None
