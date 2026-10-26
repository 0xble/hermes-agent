"""Start plan-mode progress streams without replaying ambiguous creations."""

import copy
from typing import Any


async def start_native_task_card_stream(client: Any, stream: Any, metadata: dict | None) -> None:
    """Start a card in its thread and retain Slack's stream timestamp."""
    payload: dict[str, Any] = {
        "channel": stream.channel, "thread_ts": stream.thread_ts,
        "task_display_mode": "plan",
    }
    md = metadata or {}
    recipients = (
        ("recipient_team_id", ("recipient_team_id", "team_id", "slack_team_id")),
        ("recipient_user_id", ("recipient_user_id", "user_id")),
    )
    for key, sources in recipients:
        value = next((md[source] for source in sources if md.get(source)), None)
        if value:
            payload[key] = value
    # The SDK retries connection failures by default. A start can have taken
    # effect before its response is lost. Configure this request on a copy so
    # concurrent calls retain the shared client's normal retry policy.
    start_client = copy.copy(client)
    start_client.retry_handlers = []
    result = await start_client.api_call("chat.startStream", json=payload)
    if hasattr(result, "get"):
        stream.stream_ts = str(result.get("ts") or result.get("message_ts") or "")
    if not stream.stream_ts:
        raise RuntimeError("Slack startStream returned no stream timestamp")
    stream.message_timestamps.append(stream.stream_ts)
