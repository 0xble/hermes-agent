"""Slack's native Stop button (``agent_session_stopped``) mapped onto the gateway's ``/stop``.

Slack shows a Stop control while an agent session is ``processing`` (any app that streams or
sets a thread status takes part in sessions). Clicking it sends ``agent_session_stopped`` and
nothing else: the work keeps running and the session reads "Stopping…" until the app moves it
off ``processing`` (https://docs.slack.dev/reference/events/agent_session_stopped/).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def handle_agent_session_stopped(adapter: Any, event: dict, body: Optional[dict] = None) -> None:
    channel_id = str(event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or "")
    user_id = str(event.get("user") or "")
    if not (channel_id and thread_ts and user_id):
        logger.debug("[Slack] agent_session_stopped without channel/thread/user: %s", event)
        return
    team_id = adapter._event_team_id(event, body)
    # A click from a gated channel or an unauthorized user changes nothing, status included.
    if adapter._slash_channel_gated(channel_id) or adapter._early_reject_unauthorized(
            user_id, channel_id, channel_id.startswith("D"), thread_id=thread_ts):
        return
    metadata = {"thread_id": thread_ts, "slack_team_id": team_id}
    owner = getattr(adapter, "_typing_refresh_owners", {}).get(adapter._typing_scope_key(channel_id, metadata))
    # A typed /stop in the same thread: same session key and command handling.
    await adapter._handle_slash_command({
        "command": "/stop", "text": "", "user_id": user_id, "channel_id": channel_id,
        "team_id": team_id, "thread_ts": thread_ts})
    logger.info("[Slack] agent_session_stopped channel=%s thread=%s team=%s outcome=stop_dispatched",
                channel_id, thread_ts, team_id)
    # Dispatch can admit a successor. Its processing session no longer belongs to this stop.
    if not adapter._typing_owner_retired(channel_id, metadata, owner or asyncio.current_task()):
        await adapter.stop_typing(channel_id, metadata)