"""Dedicated bounded worker boundary for synchronous SessionStore reads."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="session-store")


def session_store_executor() -> ThreadPoolExecutor:
    return _EXECUTOR


async def run_store_lookup(store: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous store lookup outside the gateway loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, partial(getattr(store, method), *args, **kwargs))


async def run_store_call(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous store-related callable outside the gateway loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, partial(function, *args, **kwargs))


__all__ = ["run_store_call", "run_store_lookup", "session_store_executor"]