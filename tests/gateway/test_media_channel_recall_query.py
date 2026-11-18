"""Media enrichment belongs to the current turn, not to channel backfill."""
import json
from unittest.mock import AsyncMock

import pytest

from agent.attachment_context import CHANNEL_CONTEXT_HEADERS, CHANNEL_MESSAGE_BOUNDARY
from agent.synthetic_prompt import auto_recall_query
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("kind", ["voice", "image"])
async def test_thread_media_content_survives_auto_recall(tmp_path, monkeypatch, shared, kind):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(thread_sessions_per_user=not shared)
    runner.adapters = {}
    runner._should_echo_stt_transcripts = lambda: False
    runner._decide_image_input_mode = lambda **kwargs: "text"
    runner._resolve_session_agent_runtime_async = AsyncMock(return_value=("test-model", {}))
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel", chat_type="group",
        thread_id="thread", user_id="user", user_name="Brian",
    )
    words = "What deadlines did we agree on?"
    path = tmp_path / ("voice.ogg" if kind == "voice" else "image.png")
    path.write_bytes(b"fixture")
    monkeypatch.setattr("tools.transcription_tools.transcribe_audio", lambda *args: {
        "success": True, "transcript": words,
    })
    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", AsyncMock(return_value=json.dumps({
        "success": True, "analysis": words,
    })))
    event = MessageEvent(
        text="", source=source,
        message_type=MessageType.VOICE if kind == "voice" else MessageType.PHOTO,
        media_urls=[str(path)], media_types=["audio/ogg" if kind == "voice" else "image/png"],
        channel_context=CHANNEL_CONTEXT_HEADERS["recent"] + "\nSomeone: old backfill",
    )
    prepared = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert prepared is not None
    query = auto_recall_query(prepared)
    assert words in query
    assert "old backfill" not in query
    assert event.channel_context in prepared
    assert words in prepared
    # Same sender/media bytes as a context-free turn; only the backfill's
    # position changes. This also covers shared-session image-only fallback.
    event.channel_context = ""
    current = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert current is not None
    assert prepared == (
        CHANNEL_CONTEXT_HEADERS["recent"] + "\nSomeone: old backfill\n"
        + CHANNEL_MESSAGE_BOUNDARY + current
    )
    if shared:
        assert current.endswith("[Brian] ")
    assert words in auto_recall_query(current)
