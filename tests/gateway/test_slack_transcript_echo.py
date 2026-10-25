"""Voice echoes reach the real Slack send path without changing the turn stream."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.slack.adapter import SlackAdapter


def _setup():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ts": "2.0"}))
    adapter._app = MagicMock()
    adapter._running = True
    adapter._get_client = MagicMock(return_value=client)
    adapter.stop_typing = AsyncMock()
    source = SessionSource(
        platform=Platform.SLACK, chat_id="D1", thread_id="1.0", user_name="Uploader",
    )
    event = MessageEvent(source=source, text="", message_type=MessageType.VOICE)
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(stt_echo_transcripts=True)
    return runner, adapter, client, source, event


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript", [
    '*Brian Le:* Keep <@U123> & *literal* `code` exactly.\nNext line.',
    '*Brian Le:* First\n*Mahin:* Second',
    'Unlabelled ' + ('<&*`"\n' * 8000),
], ids=["named", "multi-speaker", "long-literal"])
async def test_gateway_echo_posts_native_context_and_verbatim_body(transcript):
    runner, adapter, client, source, event = _setup()
    meta = {"thread_id": "1.0", "team_id": "T1", "_interim_send": True}
    seal = adapter._try_finalize_stream = AsyncMock(return_value=None)

    await runner._echo_stt_transcripts(adapter, source, [transcript], event=event, metadata=meta)

    messages = [c.kwargs for c in client.chat_postMessage.await_args_list]
    assert messages
    body = ""
    for msg in messages:
        assert msg["thread_ts"] == "1.0"
        assert msg["blocks"][0]["type"] == "context"
        context = msg["blocks"][0]["elements"][0]["text"]
        assert "Slack" in context
        assert "🎙" in context
        assert len(msg["blocks"]) <= 50
        for block in msg["blocks"][1:]:
            assert block["type"] == "section"
            assert block["text"]["type"] == "plain_text"
            assert len(block["text"]["text"]) <= 3000
            body += block["text"]["text"]
    if transcript.startswith('*Brian Le:* ') and '*Mahin:*' not in transcript:
        assert "Brian Le" in context
        assert body == transcript.removeprefix('*Brian Le:* ')
    elif "*Mahin:*" in transcript:
        assert "Brian Le, Mahin" in context
        assert body == transcript
    else:
        assert "Shared by Uploader" in context
        assert body == transcript
    fallback = "".join(msg["text"].split("\n\n", 1)[1] for msg in messages)
    assert fallback == transcript
    assert meta == {"thread_id": "1.0", "team_id": "T1", "_interim_send": True}
    assert seal.await_args.args[2]["_interim_send"] is True
    assert adapter._get_client.call_args.kwargs["team_id"] == "T1"


@pytest.mark.asyncio
async def test_pending_echo_keeps_repeated_utterances_and_deduplicates_replays():
    runner, adapter, client, source, event = _setup()
    meta = {"thread_id": "1.0", "_interim_send": True}
    for transcripts in (["same"], ["same", "same"], ["same", "same"]):
        await runner._echo_pending_stt_transcripts_once(
            event, adapter, source, transcripts, metadata=meta,
        )
    assert client.chat_postMessage.await_count == 2
    for call in client.chat_postMessage.await_args_list:
        assert call.kwargs["blocks"][1]["text"]["text"] == "same"
