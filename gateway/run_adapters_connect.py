"""Bounded platform connect that first waits for a predecessor's credential retirement."""

import asyncio


async def connect_after_retirement(adapter, is_reconnect: bool, predecessor) -> bool:
    # Queuing before teardown remains essential for wedged closes. An adapter
    # that still owns credential-using cleanup supplies its actual retirement
    # task: a same-PID lock reacquire alone cannot exclude that old consumer.
    retirement = getattr(predecessor, "_platform_lock_retirement_task", None)
    if asyncio.isfuture(retirement):
        await asyncio.gather(asyncio.shield(retirement), return_exceptions=True)
    return await adapter.connect(is_reconnect=is_reconnect)


async def connect_adapter_bounded(
    runner, adapter, platform, *, is_reconnect: bool, initial: bool, predecessor=None
) -> bool:
    timeout = runner._platform_connect_timeout_secs(platform, initial=initial)
    connect = connect_after_retirement(adapter, is_reconnect, predecessor)
    if timeout <= 0:
        return await connect
    task = asyncio.ensure_future(connect)
    if await runner._wait_or_detach(task, timeout):
        return bool(await task)
    raise TimeoutError(f"{platform.value} connect timed out after {timeout:g}s")


async def connect_adapter_with_timeout(
    runner, adapter, platform, *, is_reconnect: bool = False, initial: bool = False, predecessor=None
) -> bool:
    """Connect with a bound so one platform can't block others. ``is_reconnect``: cold boot
    drops the stale server-side queue, a reconnect keeps it. ``initial``: capped budget.

    ``is_reconnect`` is forwarded to ``adapter.connect()`` so platform adapters can distinguish a cold
    first boot (drop any stale server-side queue) from a watcher reconnect after a prolonged outage
    (preserve the queue so messages sent during the outage are delivered rather than silently dropped —
    #46621).
    ``initial`` selects the capped cold-start budget for platforms whose full connect budget is too long
    to spend before the gateway reaches ``running`` (#85993 — Telegram's 180s).
    """
    from hermes_cli.observability.shared_metrics_gateway import record_platform_connect
    try:
        ok = await runner._connect_adapter_bounded(
            adapter, platform, is_reconnect=is_reconnect, initial=initial, predecessor=predecessor)
    except Exception as exc:
        record_platform_connect(adapter, platform, is_reconnect=is_reconnect, ok=False, exc=exc)
        raise
    record_platform_connect(adapter, platform, is_reconnect=is_reconnect, ok=bool(ok))
    return ok
