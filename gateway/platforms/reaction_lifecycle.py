"""Ownership of a replacing transport's reaction slot during an active turn."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ProcessingReaction:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    explicit: bool = False


@asynccontextmanager
async def reaction_operation(
    adapter: Any, chat_id: str, message_id: str, *, author: str = "",
    start: bool = False, complete: bool = False, event: Any = None,
) -> AsyncIterator[ProcessingReaction]:
    """Serialize status and explicit writes to one target, retaining only active turns.

    Explicit calls outside a processing lifecycle do not allocate retained state.
    Signal includes the author because timestamps alone are not message identities.
    Photon uses its existing chat normalizer so latest-inbound aliases share ownership.
    """
    normalize = getattr(adapter, "_normalize_chat_key", str)
    key = (normalize(chat_id), str(message_id), str(author))
    states = adapter.__dict__.setdefault("_processing_reaction_states", {})
    state = states.get(key)
    previous = getattr(event, "_processing_reaction_state", None)
    if state is None and not start and previous and previous[0] is adapter and previous[1] == key:
        state = previous[2]
    if state is None:
        state = ProcessingReaction()
        if start:
            states[key] = state
    if event is not None:
        # Completion may run again after a post-delivery cleanup error. The event
        # retains its receipt until it dies, without retaining historical targets.
        event._processing_reaction_state = (adapter, key, state)
    try:
        async with state.lock:
            yield state
    except BaseException:
        if start and states.get(key) is state:
            states.pop(key)
        raise
    finally:
        if complete and states.get(key) is state:
            states.pop(key)
