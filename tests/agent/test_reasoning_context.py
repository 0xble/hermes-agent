import asyncio

from agent.reasoning_context import TurnReasoningContext


def test_turn_reasoning_context_restores_after_success_and_failure():
    context = TurnReasoningContext()
    owner = object()

    token = context.begin(owner, {"enabled": True, "effort": "high"})
    assert context.current(owner) == {"enabled": True, "effort": "high"}
    context.end(token)
    assert context.current(owner) is None

    token = context.begin(owner, {"enabled": True, "effort": "low"})
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        context.end(token)
    assert context.current(owner) is None


def test_turn_reasoning_context_isolates_concurrent_owners():
    context = TurnReasoningContext()
    owner_a = object()
    owner_b = object()

    async def worker(owner, effort):
        token = context.begin(owner, {"enabled": True, "effort": effort})
        await asyncio.sleep(0)
        observed = context.current(owner)
        context.end(token)
        return observed

    async def run():
        return await asyncio.gather(
            worker(owner_a, "high"),
            worker(owner_b, "low"),
        )

    assert asyncio.run(run()) == [
        {"enabled": True, "effort": "high"},
        {"enabled": True, "effort": "low"},
    ]
    assert context.current(owner_a) is None
    assert context.current(owner_b) is None
