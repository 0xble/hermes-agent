"""The explicit Telegram meetings skill binding opts a topic into the live lane.

Keep this event-scoped: titles are labels, not policy, and profile/global busy and
attended configuration must remain unchanged.
"""

import asyncio

from gateway.config import Platform
from gateway.platforms.event import MessageType
from gateway.response_filters import is_passive_fyi_relay_event


def is_meeting_lane(event) -> bool:
    source = event.source
    skills = event.auto_skill
    return (
        source.platform == Platform.TELEGRAM
        and bool(source.thread_id)
        and ("meetings" in skills if isinstance(skills, list) else skills == "meetings")
    )


async def apply_reasoning_default(runner, event, session_key: str) -> None:
    """Use the existing durable session pick; never replace a user-selected effort."""
    if (not is_meeting_lane(event) or event.internal or event.get_command()
            or getattr(runner, "session_store", None) is None):
        return
    await asyncio.to_thread(_apply_reasoning_default, runner, event, session_key)


def _apply_reasoning_default(runner, event, session_key: str) -> None:
    if runner._session_reasoning_override(session_key) is None:
        runner._set_reasoning_override(
            session_key, {"enabled": True, "effort": "medium"}, event.source, scope="session",
        )


async def busy_input_mode(runner, event, session_key: str) -> str:
    """One policy at adapter, priority and durable admission boundaries.

    Only ordinary text can opt into steer. Existing internal, drain, FYI, media
    and compression paths retain their configured behavior and safety guards.
    """
    mode = runner._effective_busy_input_mode(event.source)
    if (
        not is_meeting_lane(event) or event.internal or event.get_command()
        or event.message_type != MessageType.TEXT or event.media_urls or event.media_types
        or runner._draining
        or is_passive_fyi_relay_event(
            event, event.source, enabled=bool(runner._relay_fyi_passive_enabled(event.source)),
        )
    ):
        return mode
    if await runner._session_has_compression_in_flight(session_key):
        return "queue"
    await apply_reasoning_default(runner, event, session_key)
    return "steer"
