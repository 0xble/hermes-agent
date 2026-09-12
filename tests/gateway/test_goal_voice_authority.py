"""Exercise real STT preprocessing and authenticated goal scope together."""
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals
from tools.goal_authority import goal_authorization_task
from tools.goal_tool import set_goal_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("successful", [True, False])
async def test_voice_stt_then_authority_releases_only_current_successful_transcript(tmp_path, monkeypatch, successful):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")})
    runner.adapters = {}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="voice", chat_type="private", user_name="User")
    event = MessageEvent(text="", source=source, message_type=MessageType.VOICE,
                         media_urls=["/tmp/current.ogg"], media_types=["audio/ogg"],
                         reply_to_text="Resume from this quoted text", reply_to_message_id="old")
    transcript = "Continue the implementation."
    runner._enrich_message_with_transcription = AsyncMock(return_value=(
        transcript if successful else "[STT failed: continue anyway]",
        [transcript] if successful else [],
    ))
    rendered = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert "quoted text" in rendered
    runner._enrich_message_with_transcription.assert_awaited_once()
    authority = runner._goal_authority_text_for_event(event, typed_text=event.text or "")
    assert authority == (transcript if successful else "")
    goals.GoalManager("voice").set("Implementation")
    goals.GoalManager("voice").pause(user_requested=True)

    async def inner(*args, **kwargs):
        return await runner._run_in_executor_with_context(lambda: json.loads(set_goal_tool(
            action="resume", session_id="voice", turn_id="turn", user_requested=True,
            goal_control_revision=goals.get_goal_control_revision("voice"),
        )))

    runner._run_agent_inner = inner
    result = await runner._run_agent(rendered, "", [], source, "voice", goal_user_text=authority)
    assert result["success"] is successful
    assert goals.load_goal("voice").status == ("active" if successful else "paused")
    assert goal_authorization_task("voice", "outside") == "outside"
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_queued_voice_followup_binds_successful_current_event_stt():
    runner = MagicMock()
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._run_agent = AsyncMock(return_value={"final_response": "done", "messages": []})
    runner._is_goal_continuation_event.return_value = False
    runner._session_key_for_source.return_value = "voice"
    async def prepare_queued(*args, **kwargs):
        kwargs["event"].text = "[auto-loaded queued skill text]"
        return "Continue queued work."

    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(side_effect=prepare_queued)
    runner._reply_anchor_for_event.return_value = None
    runner._adapter_for_source.return_value = None
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._goal_authority_text_for_event = GatewayRunner._goal_authority_text_for_event
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="voice", chat_type="private", user_name="User",
    )
    turn_ctx = SimpleNamespace(
        source=source, session_id="voice", session_key="voice", run_generation=1,
        _interrupt_depth=0, history=[], _status_thread_metadata={}, context_prompt=None,
        result_holder=[None],
    )
    pending_event = MessageEvent(
        text="", source=source, message_type=MessageType.VOICE, message_id="queued-voice",
    )
    setattr(pending_event, "_gateway_goal_authority_transcripts", ["Continue queued work."])

    await GatewayRunner._run_agent_queued_followup(
        runner, cast(Any, turn_ctx), adapter=None, pending="Continue queued work.",
        pending_event=pending_event, response="", result={"interrupted": True, "messages": []},
        stream_task=None,
    )

    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["goal_user_text"] == "Continue queued work."


def test_internal_event_cannot_bind_stt_authority():
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="voice", chat_type="private", user_name="User",
    )
    event = MessageEvent(text="Continue.", source=source, internal=True)
    setattr(event, "_gateway_goal_authority_transcripts", ["Resume."])

    assert GatewayRunner._goal_authority_text_for_event(event, typed_text=event.text or "") == ""


def test_typed_caption_and_successful_stt_are_both_current_user_authority():
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="voice", chat_type="private", user_name="User",
    )
    event = MessageEvent(text="Please continue.", source=source)
    setattr(event, "_gateway_goal_authority_transcripts", ["Use the current branch."])

    assert GatewayRunner._goal_authority_text_for_event(event, typed_text=event.text or "") == (
        "Please continue.\n\nUse the current branch."
    )


@pytest.mark.asyncio
async def test_pending_voice_transcription_stamps_authority_for_queued_turn():
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="voice", chat_type="private", user_name="User",
    )
    event = MessageEvent(
        text="", source=source, message_type=MessageType.VOICE,
        media_urls=["/tmp/queued.ogg"], media_types=["audio/ogg"],
    )
    runner._enrich_message_with_transcription = AsyncMock(
        return_value=("Continue queued work.", ["Continue queued work."]),
    )

    text, transcripts = await runner._transcribe_pending_audio_event_once(event)

    assert text == "Continue queued work."
    assert transcripts == ["Continue queued work."]
    assert runner._goal_authority_text_for_event(
        event, typed_text=event.text or "",
    ) == "Continue queued work."


def test_merging_pending_media_invalidates_goal_authority_transcripts():
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="voice", chat_type="private", user_name="User",
    )
    existing = MessageEvent(
        text="", source=source, message_type=MessageType.VOICE,
        media_urls=["/tmp/first.ogg"], media_types=["audio/ogg"],
    )
    setattr(existing, "_gateway_pending_stt_text", "First transcript")
    setattr(existing, "_gateway_pending_stt_transcripts", ["First transcript"])
    setattr(existing, "_gateway_goal_authority_transcripts", ["First transcript"])
    incoming = MessageEvent(
        text="", source=source, message_type=MessageType.VOICE,
        media_urls=["/tmp/second.ogg"], media_types=["audio/ogg"],
    )
    pending = {"voice": existing}

    merge_pending_message_event(pending, "voice", incoming)

    assert not hasattr(existing, "_gateway_pending_stt_text")
    assert not hasattr(existing, "_gateway_pending_stt_transcripts")
    assert not hasattr(existing, "_gateway_goal_authority_transcripts")
