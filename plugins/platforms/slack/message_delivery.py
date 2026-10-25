"""Slack text delivery, including literal speech-to-text cards."""

from __future__ import annotations

import re
from typing import Any, Optional


_SPEAKER = re.compile(r"^\*([^*\n]{1,200}):\*", re.MULTILINE)


def transcript_messages(transcript: str, shared_by: str):
    """Yield bounded cards. Only explicit single-speaker labels move out of the body."""
    labels = list(_SPEAKER.finditer(transcript))
    body = transcript
    label = "Slack voice transcript"
    if len(labels) == 1 and labels[0].start() == 0:
        label = f"{labels[0].group(1)} · {label}"
        body = transcript[labels[0].end():]
        if body.startswith(" "):
            body = body[1:]
    elif labels and labels[0].start() == 0:
        speakers = ", ".join(dict.fromkeys(match.group(1) for match in labels))
        label = f"{speakers} · {label}"
    elif shared_by:
        # A file's uploader is not necessarily a voice in its recording.
        label += f" · Shared by {shared_by}"
    label = label[:2900]
    context = {"type": "context", "elements": [
        {"type": "plain_text", "text": f"🎙 {label}"[:3000], "emoji": False},
    ]}
    # Leave room for metadata in Slack's 40k fallback text limit, and stay below 50 blocks.
    for offset in range(0, max(len(body), 1), 36000):
        part = body[offset:offset + 36000]
        blocks = [context] + [
            {"type": "section", "text": {"type": "plain_text", "text": part[i:i + 3000], "emoji": False}}
            for i in range(0, len(part), 3000)
        ]
        # Preserve the original attributed transcript in the accessible fallback.
        prefix = transcript[:len(transcript) - len(body)] if offset == 0 else ""
        fallback = prefix + part
        yield {"text": f"{label}\n\n{fallback}", "blocks": blocks, "mrkdwn": False}


async def post_chunks(
    adapter, chat_id: str, team_id: str, content: str, formatted: str,
    thread_ts: Optional[str], metadata: Optional[dict[str, Any]],
) -> Any:
    """Post text or transcript cards through the same workspace and block-rejection fallback."""
    # Late import preserves the plugin's standalone import path and the existing config coercion.
    from .adapter import _slack_unfurl_kwargs

    transcript = (metadata or {}).get("_stt_transcript")
    if transcript is not None:
        messages = transcript_messages(transcript["text"], transcript["shared_by"])
    else:
        chunks = adapter.truncate_message(formatted, adapter.MAX_MESSAGE_LENGTH)
        blocks = adapter._maybe_blocks(content, metadata) if len(chunks) == 1 else None
        messages = [dict(text=chunk, mrkdwn=True, **({"blocks": blocks} if blocks and i == 0 else {}))
                    for i, chunk in enumerate(chunks)]
    if transcript is None and blocks and blocks[0]["type"] == "context":
        from gateway.platforms.helpers import strip_markdown
        messages[0]["text"] = strip_markdown(content)
    broadcast = adapter.config.extra.get("reply_broadcast", False)
    last_result = None
    for i, message in enumerate(messages):
        kwargs = {"channel": chat_id, **message, **_slack_unfurl_kwargs(adapter.config.extra)}
        if transcript is not None:
            kwargs.update(unfurl_links=False, unfurl_media=False, parse="none")
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
            if broadcast and i == 0:
                kwargs["reply_broadcast"] = True
        client_fn = lambda: adapter._get_client(chat_id, team_id=team_id)
        last_result = await adapter._call_with_block_fallback(client_fn, "chat_postMessage", kwargs, "send")
    return last_result
