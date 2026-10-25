"""Cleanup for native progress cards, never the assistant's reply stream."""

import logging

logger = logging.getLogger(__name__)


async def finish_task_card(client, stream, *, delete: bool) -> None:
    """Seal first so a failed deletion cannot leave Slack's live indicator armed."""
    # Adapter discovery must remain importable without the optional Slack SDK.
    from slack_sdk.errors import SlackApiError

    if stream.stream_ts:
        try:
            await client.api_call("chat.stopStream", json={"channel": stream.channel, "ts": stream.stream_ts})
        except Exception as exc:
            # Slack may already have sealed an expired stream. Still try deletion.
            logger.debug("[Slack] Native task-card stopStream failed: %s", exc)
    if not delete:
        return
    for ts in stream.message_timestamps:
        try:
            await client.api_call("chat.delete", json={"channel": stream.channel, "ts": ts})
        except SlackApiError as exc:
            if exc.response.get("error") != "message_not_found":
                logger.warning("[Slack] Native task-card deletion failed for %s: %s", ts, exc)
        except Exception as exc:
            # Progress cleanup must never prevent delivery of the real reply.
            logger.warning("[Slack] Native task-card deletion failed for %s: %s", ts, exc)
