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
