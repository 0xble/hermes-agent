"""Owner-home-DM tombstone cleanup for Slack ``message_changed`` events."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapter import SlackAdapter


async def process_changed_message(
    adapter: SlackAdapter, event: dict, payload: dict | None,
) -> dict | None:
    changed = event.get("message")
    if isinstance(changed, dict) and changed.get("subtype") == "tombstone":
        await _cascade_delete_tombstoned_thread(adapter, event, payload)
        return None
    return adapter._normalize_changed_message(event)


def _cleanup_scope(
    adapter: SlackAdapter, event: dict, payload: dict | None,
) -> tuple[str, str, str, str] | None:
    message = event["message"]
    root_ts = str(message.get("ts") or "")
    channel_id = str(event.get("channel") or "")
    team_id = adapter._event_team_id(event, payload)
    home = adapter.config.home_channel
    if (
        not home or not home.user_id or not home.scope_id
        or event.get("channel_type") != "im" or not channel_id.startswith("D")
        or channel_id != home.chat_id or team_id != home.scope_id
        or not root_ts or message.get("thread_ts") != root_ts
        or team_id not in adapter._team_clients or adapter._is_ignored_channel(channel_id)
    ):
        return None
    return team_id, channel_id, root_ts, home.user_id


async def _read_thread(bot_client, channel_id: str, root_ts: str) -> tuple[list[dict], str]:
    try:
        from .adapter import _slack_response_payload
    except ImportError:  # pragma: no cover - flat plugin-dir import
        from adapter import _slack_response_payload

    replies = []
    cursor = ""
    while True:
        page = _slack_response_payload(await bot_client.conversations_replies(
            channel=channel_id, ts=root_ts, limit=200, cursor=cursor))
        if not page.get("ok"):
            return [], f"read thread: {page.get('error') or 'unknown_error'}"
        messages = page.get("messages") or []
        if not cursor and not any(
            m.get("ts") == root_ts and m.get("subtype") == "tombstone" for m in messages
        ):
            return [], ""
        replies.extend(m for m in messages if m.get("ts") != root_ts)
        next_cursor = str((page.get("response_metadata") or {}).get("next_cursor") or "")
        if not next_cursor:
            return replies, ""
        if next_cursor == cursor:
            return [], "read thread: repeated_cursor"
        cursor = next_cursor


def _failure_detail(exc: BaseException) -> str:
    try:
        from .adapter import _slack_response_payload
    except ImportError:  # pragma: no cover - flat plugin-dir import
        from adapter import _slack_response_payload

    # Raw SDK exception strings can include tokens or message bodies.
    return _slack_response_payload(getattr(exc, "response", None)).get("error") or type(exc).__name__


async def _delete_replies(
    replies: list[dict], bot_client, user_client, bot_user_id: str, owner_id: str,
    channel_id: str, root_ts: str,
) -> list[str]:
    from aiohttp import ClientError
    from slack_sdk.errors import SlackClientError

    try:
        from .adapter import _slack_response_payload
    except ImportError:  # pragma: no cover - flat plugin-dir import
        from adapter import _slack_response_payload

    failures = []
    clients = {bot_user_id: bot_client, owner_id: user_client}
    for reply in replies:
        ts = str(reply.get("ts") or "")
        if not ts or reply.get("thread_ts") != root_ts:
            failures.append(f"reply {ts or 'unknown'}: not in deleted thread")
            continue
        client = clients.get(str(reply.get("user") or ""))
        if client is None:
            failures.append(f"reply {ts}: unsupported author")
            continue
        try:
            result = _slack_response_payload(await client.chat_delete(channel=channel_id, ts=ts))
            if not result.get("ok"):
                failures.append(f"reply {ts}: {result.get('error') or 'unknown_error'}")
        except (SlackClientError, ClientError, OSError, TimeoutError) as exc:
            failures.append(f"reply {ts}: {_failure_detail(exc)}")
    return failures


async def _report_failures(bot_client, channel_id: str, root_ts: str, failures: list[str]) -> None:
    from aiohttp import ClientError
    from slack_sdk.errors import SlackClientError

    try:
        from .adapter import logger
    except ImportError:  # pragma: no cover - flat plugin-dir import
        from adapter import logger

    details = list(dict.fromkeys(failures))
    summary = "; ".join(details[:3])
    if len(details) > 3:
        summary += f"; {len(details) - 3} more failures"
    try:
        await bot_client.chat_postMessage(
            channel=channel_id, text=f"Couldn't clear deleted thread {root_ts}: {summary}",
            unfurl_links=False, unfurl_media=False,
        )
    except (SlackClientError, ClientError, OSError, TimeoutError) as exc:
        logger.warning("[Slack] Cascade-delete failure notice could not be sent (%s)", type(exc).__name__)


async def _cascade_delete_tombstoned_thread(
    adapter: SlackAdapter, event: dict, payload: dict | None,
) -> None:
    """Parent authorship is irrelevant: the owner can delete either author's root."""
    from aiohttp import ClientError
    from slack_sdk.errors import SlackClientError

    try:
        from .adapter import _get_scoped_secret, _slack_response_payload
    except ImportError:  # pragma: no cover - flat plugin-dir import
        from adapter import _get_scoped_secret, _slack_response_payload

    scope = _cleanup_scope(adapter, event, payload)
    if scope is None:
        return
    team_id, channel_id, root_ts, owner_id = scope
    # Reply deletion changes the tombstone again. Claim the root before any await
    # so concurrent updates/redelivery cannot repeat deletes or failure notices.
    if adapter._dedup.is_duplicate(adapter._workspace_event_id(team_id, f"cascade:{channel_id}:{root_ts}")):
        return
    bot_client = adapter._team_clients[team_id]
    failures = []
    operation = "check owner DM"
    try:
        info = _slack_response_payload(await bot_client.conversations_info(channel=channel_id))
        if not info.get("ok"):
            failures.append(f"check owner DM: {info.get('error') or 'unknown_error'}")
            return
        channel = info.get("channel") or {}
        if not channel.get("is_im") or channel.get("user") != owner_id:
            return
        operation = "read thread"
        # Complete pagination before mutation. Failed reads never delete an
        # incomplete snapshot, and a stale event for a live root deletes nothing.
        replies, read_failure = await _read_thread(bot_client, channel_id, root_ts)
        if read_failure:
            failures.append(read_failure)
        if read_failure or not replies:
            return
        operation = "authenticate owner token"
        token = _get_scoped_secret("SLACK_USER_TOKEN")
        if not token:
            failures.append("SLACK_USER_TOKEN is not configured")
            return
        user_client = adapter._new_web_client(token, adapter._proxy_url)
        user_client.retry_handlers = []
        auth = _slack_response_payload(await user_client.auth_test())
        if (
            not auth.get("ok") or auth.get("team_id") != team_id
            or auth.get("user_id") != owner_id or auth.get("bot_id")
        ):
            failures.append("SLACK_USER_TOKEN must belong to the owner in this workspace")
            return
        failures.extend(await _delete_replies(
            replies, bot_client, user_client, adapter._team_bot_user_ids[team_id], owner_id,
            channel_id, root_ts,
        ))
    except (SlackClientError, ClientError, OSError, TimeoutError) as exc:
        failures.append(f"{operation}: {_failure_detail(exc)}")
    finally:
        if failures:
            await _report_failures(bot_client, channel_id, root_ts, failures)
