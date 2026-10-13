"""Attachment enrichment must not spend the automatic recall budget on scaffolding."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.synthetic_prompt import auto_recall_query, human_prompt_text
from agent.turn_context import _memory_turn_start_and_prefetch
from gateway.run_inbound import GatewayInboundMixin
from tests.agent.test_synthetic_prompt import (
    _goal_prompt, _post_turn_agent, _process_notice, _turn_agent,
)

REQUEST = "Tell me how to do this in my electric car"
DESCRIPTION = (
    'A phone screenshot shows a conversation about a fogged windshield. '
    'The quoted message says "I cannot see the road" and asks about the wipers. '
    'A dashboard screen is visible; there is no readable model label. '
) * 6
PATH = "/cache/images/windshield.jpg"


@pytest.fixture
def enrich(monkeypatch):
    result = AsyncMock(return_value=json.dumps({"success": True, "analysis": DESCRIPTION}))
    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", result)
    runner = GatewayInboundMixin.__new__(GatewayInboundMixin)
    return lambda caption, paths=None: asyncio.run(runner._enrich_message_with_vision(
        caption, paths or [PATH]))


@pytest.mark.parametrize("platform", ["telegram", "discord"])
@pytest.mark.parametrize("own_reply", [False, True])
@pytest.mark.parametrize("caption", [REQUEST, "", "thanks!"])
def test_gateway_reply_and_trigger_wrappers_do_not_hide_attachment_caption(
    enrich, monkeypatch, platform, own_reply, caption,
):
    from gateway.config import Platform

    monkeypatch.setattr("gateway.session._discord_tools_loaded", lambda: True)
    event = SimpleNamespace(
        message_id="42", reply_to_message_id="41",
        reply_to_text='Previous message with [brackets] and "quotes".\n\nMore context.',
        reply_to_is_own_message=own_reply,
    )
    source = SimpleNamespace(platform=Platform(platform))
    prompt = GatewayInboundMixin._prepend_inbound_reply_context(event, source, enrich(caption))
    if caption:
        assert prompt.index(caption) > 800
    expected = DESCRIPTION.strip() if not caption else "" if caption == "thanks!" else caption
    assert auto_recall_query(prompt, platform=platform) == expected
    assert human_prompt_text(prompt) == prompt
    agent = _turn_agent()
    _memory_turn_start_and_prefetch(agent, prompt)
    if expected:
        assert agent._memory_manager.prefetch_all.call_args.args[0] == expected
    else:
        agent._memory_manager.prefetch_all.assert_not_called()
    queued = _post_turn_agent(display_kind=None)
    queued._sync_external_memory_for_turn(
        original_user_message=prompt, final_response="answer", interrupted=False,
    )
    if expected:
        assert queued._memory_manager.queue_prefetch_all.call_args.args[0] == expected
    else:
        queued._memory_manager.queue_prefetch_all.assert_not_called()


@pytest.mark.parametrize("platform", ["telegram", "discord"])
def test_reply_context_delimiter_collision_preserves_the_quote_and_caption(enrich, monkeypatch, platform):
    from gateway.config import Platform

    monkeypatch.setattr("gateway.session._discord_tools_loaded", lambda: True)
    quoted = 'Available modes: ["front", "rear"]\n\nUse the rear defroster only for the rear window.'
    event = SimpleNamespace(
        message_id="42", reply_to_message_id="41", reply_to_text=quoted,
        reply_to_is_own_message=False,
    )
    source = SimpleNamespace(platform=Platform(platform))
    prompt = GatewayInboundMixin._prepend_inbound_reply_context(event, source, enrich(REQUEST))
    assert auto_recall_query(prompt, platform=platform) == REQUEST
    pointer = prompt.split('[Replying to: ', 1)[1].split(']\n\n', 1)[0]
    assert json.loads(pointer) == quoted
    immediate = _turn_agent(platform=platform)
    _memory_turn_start_and_prefetch(immediate, prompt)
    immediate._memory_manager.prefetch_all.assert_called_once_with(REQUEST, session_id=immediate.session_id)
    queued = _post_turn_agent(display_kind=None, platform=platform)
    queued._sync_external_memory_for_turn(
        original_user_message=prompt, final_response="answer", interrupted=False,
    )
    queued._memory_manager.queue_prefetch_all.assert_called_once_with(REQUEST, session_id=queued.session_id)


def test_discord_trigger_without_reply_does_not_hide_attachment_caption(enrich, monkeypatch):
    from gateway.config import Platform

    monkeypatch.setattr("gateway.session._discord_tools_loaded", lambda: True)
    event = SimpleNamespace(message_id="42", reply_to_text=None, reply_to_message_id=None)
    source = SimpleNamespace(platform=Platform.DISCORD)
    prompt = GatewayInboundMixin._prepend_inbound_reply_context(event, source, enrich(REQUEST))
    assert auto_recall_query(prompt, platform="discord") == REQUEST


def test_reply_pointer_cannot_override_generated_turn_provenance(enrich):
    event = SimpleNamespace(message_id="42", reply_to_message_id="41", reply_to_text="Previous")
    prompt = GatewayInboundMixin._prepend_inbound_reply_context(event, None, enrich(REQUEST))
    assert auto_recall_query(prompt, display_kind="internal_notification") == ""
    assert auto_recall_query(prompt, platform="cron") == ""


@pytest.mark.parametrize("generated", [_process_notice, _goal_prompt])
def test_synthetic_recall_keeps_generated_text_but_removes_attachment_scaffolding(enrich, generated):
    before = generated()
    query = auto_recall_query(
        before + "\n\n" + enrich(REQUEST), include_synthetic=True,
    )
    assert before in query
    assert REQUEST in query
    assert DESCRIPTION not in query
    assert query.index(REQUEST) < 800


@pytest.mark.parametrize("generated", [_process_notice, _goal_prompt])
def test_synthetic_recall_removes_attachment_scaffolding_inside_enrichment(enrich, generated):
    query = auto_recall_query(
        enrich(generated() + "\n\n" + REQUEST), include_synthetic=True,
    )
    assert generated() in query
    assert REQUEST in query
    assert DESCRIPTION not in query


@pytest.mark.parametrize("generated", [_process_notice, _goal_prompt])
def test_synthetic_recall_does_not_repeat_the_request_inside_enrichment(enrich, generated):
    query = auto_recall_query(enrich(generated() + "\n\n" + REQUEST), include_synthetic=True)
    assert query.count(REQUEST) == 1
    assert query.startswith(REQUEST)
    assert generated() in query


def test_untranscribed_voice_fallback_note_is_not_the_recall_query(monkeypatch):
    """gateway/run_inbound.py emits this bare note when the transcription module is missing."""
    import builtins
    real_import = builtins.__import__

    def _no_stt(name, *args, **kwargs):
        if name == "tools.transcription_tools":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_stt)
    mixin = object.__new__(GatewayInboundMixin)
    mixin.config = SimpleNamespace(stt_enabled=True)
    enrich = getattr(mixin, "_enrich_message_with_transcription")
    prompt, _ = asyncio.run(enrich(REQUEST, ["/cache/audio/voice.ogg"]))
    assert prompt.startswith("[voice message could not be transcribed]")
    assert auto_recall_query(prompt) == REQUEST
    assert auto_recall_query(prompt, include_synthetic=True) == REQUEST


def test_long_gateway_image_prefix_recalls_the_actual_personal_request(enrich):
    prompt = enrich(REQUEST)
    assert prompt.index(REQUEST) > 800
    assert auto_recall_query(prompt, platform="telegram") == REQUEST
    # Query extraction must not change retention or the prompt delivered to the model.
    assert human_prompt_text(prompt) == prompt


@pytest.mark.parametrize("caption", ["My car?", "Mom?", "生日？", "tax?", "k8s"])
def test_short_meaningful_captions_are_not_lost(enrich, caption):
    assert auto_recall_query(enrich(caption)) == caption
    assert auto_recall_query(caption) == caption


@pytest.mark.parametrize("caption", ["ok", "thanks!", "/help"])
def test_attachment_noise_cannot_force_recall_for_a_trivial_caption(enrich, caption):
    assert auto_recall_query(enrich(caption)) == ""


def test_image_only_turn_keeps_a_meaningful_description_not_path_advice(enrich):
    assert auto_recall_query(enrich("")) == DESCRIPTION.strip()


def test_multiple_images_do_not_swallow_the_caption(enrich):
    assert auto_recall_query(enrich(REQUEST, [PATH, "/cache/images/controls.jpg"])) == REQUEST
    assert auto_recall_query(enrich("", [PATH, "/cache/images/controls.jpg"])) == (
        DESCRIPTION.strip() + "\n\n" + DESCRIPTION.strip())


def test_multiline_image_description_with_brackets_does_not_leak_into_caption(monkeypatch, enrich):
    description = "Screen [dark mode]\n\nQuoted text: [Check my records]\n\nWipers and glass."
    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", AsyncMock(
        return_value=json.dumps({"success": True, "analysis": description})))
    assert auto_recall_query(enrich(REQUEST)) == REQUEST
    assert auto_recall_query(enrich("")) == description


def test_cli_image_description_uses_the_same_extraction():
    prompt = (f"[The user attached an image. Here's what it contains:\n{DESCRIPTION}]\n"
              f"[If you need a closer look, use vision_analyze with image_url: {PATH}]\n\n{REQUEST}")
    assert auto_recall_query(prompt, platform="cli") == REQUEST


@pytest.mark.parametrize("kind,platform", [("internal_notification", "telegram"), (None, "cron"), ("auto_continue", "tui")])
def test_attachment_wrapping_cannot_override_runtime_provenance(enrich, kind, platform):
    assert auto_recall_query(enrich(REQUEST), display_kind=kind, platform=platform) == ""


@pytest.mark.parametrize("generated", [_process_notice, _goal_prompt])
def test_synthetic_prefix_and_attachment_prefix_share_the_human_boundary(enrich, generated):
    assert auto_recall_query(generated() + "\n\n" + enrich(REQUEST)) == REQUEST
    assert auto_recall_query(enrich(generated() + "\n\n" + REQUEST)) == REQUEST
    # No image-only fallback when what follows the image is a generated notice.
    assert auto_recall_query(enrich(generated())) == ""


@pytest.mark.parametrize("success", [False, "error"])
def test_failed_image_analysis_keeps_caption_but_cannot_query_its_error(enrich, monkeypatch, success):
    mock = AsyncMock(side_effect=RuntimeError("vision unavailable")) if success == "error" else AsyncMock(
        return_value=json.dumps({"success": False}))
    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", mock)
    assert auto_recall_query(enrich(REQUEST)) == REQUEST
    assert auto_recall_query(enrich("")) == ""


def test_image_path_placeholder_has_no_semantic_query():
    from gateway.run import _build_media_placeholder
    from gateway.platforms.event import MessageType

    prompt = _build_media_placeholder(SimpleNamespace(media_urls=[PATH], media_types=["image/jpeg"],
                                                     message_type=MessageType.PHOTO))
    assert auto_recall_query(prompt) == ""
    assert auto_recall_query(prompt + "\n\n" + REQUEST) == REQUEST


def test_cached_document_and_audio_metadata_cannot_displace_request():
    from gateway.run import _build_document_context_note

    note = _build_document_context_note("long-title-" * 100 + ".pdf", "/cache/notes.pdf", "application/pdf")
    assert auto_recall_query(note + "\n\n" + REQUEST) == REQUEST
    assert auto_recall_query(note) == ""
    audio = '[The user sent a voice message: /cache/audio/voice.ogg (duration: 00:12)]'
    assert auto_recall_query(audio + "\n\n" + REQUEST) == REQUEST
    assert auto_recall_query(audio) == ""
    # Actual transcribed words are user speech, not metadata to discard.
    assert auto_recall_query('"What car do I own?"') == '"What car do I own?"'


@pytest.mark.parametrize("producer", ["tui_image", "untranscribed_audio"])
@pytest.mark.parametrize("caption", ["", REQUEST])
def test_current_attachment_producers_cannot_displace_the_caption(tmp_path, producer, caption):
    if producer == "tui_image":
        from tui_gateway.server import _build_image_ref_message

        image = tmp_path / "windshield.jpg"
        image.touch()
        prompt = _build_image_ref_message(caption, [str(image)])
    else:
        prompt = GatewayInboundMixin._untranscribed_audio_note(str(tmp_path / "voice.ogg"))
        if caption:
            prompt += "\n\n" + caption
    assert auto_recall_query(prompt) == caption


@pytest.mark.parametrize("note", [
    "[User sent a file: x]",
    "[User sent an image: /cache/image.png]",
    "[The user sent a voice message: /cache/voice.ogg (duration: 00:12)]",
])
def test_same_line_text_after_metadata_is_a_documented_limitation(note):
    """Known limitation: no producer puts human text on a note's line, and names/paths can contain
    `] `, so a single-line note ends at its line's last `]`. A hand-typed same-line lookalike is
    consumed with the note (recall skipped) rather than leaking metadata into the query."""
    assert auto_recall_query(f"{note} what about [this]") == ""
    assert auto_recall_query(f"{note} what about [this]\n\n{REQUEST}") == REQUEST


BRACKETED = "invoice [final] copy"


@pytest.mark.parametrize("producer", ["document", "text_document", "placeholder", "media_file", "tui_image"])
@pytest.mark.parametrize("caption", ["", REQUEST])
def test_bracketed_attachment_names_and_paths_stay_inside_their_metadata_note(tmp_path, producer, caption):
    from gateway.run import _build_document_context_note, _build_media_placeholder
    from gateway.platforms.event import MessageType

    if producer == "document":
        prompt = _build_document_context_note(f"{BRACKETED}.pdf", "/cache/invoice.pdf", "application/pdf")
    elif producer == "text_document":
        prompt = _build_document_context_note(f"{BRACKETED}.txt", f"/cache/{BRACKETED}.txt", "text/plain",
                                              content_inlined=False)
    elif producer == "placeholder":
        prompt = _build_media_placeholder(SimpleNamespace(
            media_urls=[f"/cache/{BRACKETED}.bin"], media_types=["application/octet-stream"],
            message_type=MessageType.DOCUMENT))
    elif producer == "media_file":
        # The display name is sanitized but the cached path keeps the original characters.
        prompt = GatewayInboundMixin._prepend_inbound_media_file_notes(
            "", [], [str(tmp_path / f"a_b_{BRACKETED}.mp4")]).strip()
    else:
        from tui_gateway.server import _build_image_ref_message

        image = tmp_path / f"{BRACKETED}.jpg"
        image.touch()
        prompt = _build_image_ref_message("", [str(image)])
    if caption:
        prompt += "\n\n" + caption
    assert auto_recall_query(prompt) == caption


def _multimodal_turn(caption, images=1):
    content = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,aGVsbG8{i}"}}
               for i in range(images)]
    if caption:
        content.append({"type": "text", "text": caption})
    return content


@pytest.mark.parametrize("caption", [
    "Does this match my car [Tesla Model 3]",
    "Context [screenshot]\n\nTell me how to do this in my electric car",
    REQUEST,
])
@pytest.mark.parametrize("images", [1, 2])
def test_native_multimodal_turn_recalls_the_complete_caption(monkeypatch, caption, images):
    """Native image turns reach recall as the stored multimodal message, not the non-vision API copy.

    The API copy's data-URL description note has no closing marker before the caption, so recall must
    never read it. Bracketed captions and caption paragraphs survive unchanged."""
    from agent.vision_message_prep import VisionMessagePrepMixin

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", AsyncMock(
        return_value=json.dumps({"analysis": DESCRIPTION})))
    turn = _multimodal_turn(caption, images)
    runner = VisionMessagePrepMixin()
    setattr(runner, "_anthropic_image_fallback_cache", {})
    api_copy = runner._preprocess_anthropic_content(json.loads(json.dumps(turn)), "user")
    assert api_copy.index(caption) > 800
    agent = _turn_agent()
    _memory_turn_start_and_prefetch(agent, turn)
    agent._memory_manager.prefetch_all.assert_called_once_with(caption, session_id="s-1")
    agent = _post_turn_agent(display_kind=None)
    agent._sync_external_memory_for_turn(original_user_message=turn, final_response="Answer.", interrupted=False)
    agent._memory_manager.queue_prefetch_all.assert_called_once_with(caption, session_id="s-1")


def test_native_image_only_turn_has_no_recall_query():
    agent = _turn_agent()
    _memory_turn_start_and_prefetch(agent, _multimodal_turn("", images=2))
    agent._memory_manager.prefetch_all.assert_not_called()


def test_unpaired_description_note_is_never_stripped_as_attachment_metadata():
    """Only producer-delimited notes are attachment scaffolding. A no-advice description note has no
    closing boundary, so recall keeps the whole text rather than guessing where a caption starts."""
    prompt = f"[The user attached an image. Here's what it contains:\n{DESCRIPTION}]\n\n{REQUEST}"
    assert auto_recall_query(prompt) == prompt


def test_hindsight_receives_the_request_with_its_existing_input_cap(enrich):
    from plugins.memory.hindsight import HindsightMemoryProvider
    from unittest.mock import Mock

    provider = HindsightMemoryProvider()
    provider._recall_max_input_chars = 800
    provider._prefetch_method = "recall"
    provider._recall = Mock(return_value=[])
    provider._do_recall(auto_recall_query(enrich(REQUEST)))
    provider._recall.assert_called_once_with(REQUEST)


def test_inline_document_body_and_transcribed_speech_are_not_discarded():
    from gateway.run import _build_document_context_note

    body = "Receipt: electric car maintenance.\n\nCheck the service records."
    note = _build_document_context_note("receipt.txt", "/cache/receipt.txt", "text/plain")
    assert auto_recall_query(note + "\n\n" + body) == body
    spoken = '"My car needs windshield service"'
    assert auto_recall_query(spoken + "\n\nWhich garage did I use?") == spoken + "\n\nWhich garage did I use?"


def test_unknown_or_incomplete_attachment_lookalike_is_preserved():
    for prompt in ("[The user sent an image~ My question is about my car]", "[The user attached an image.",
                   "[IMPORTANT: remember my car]"):
        assert auto_recall_query(prompt) == prompt


def test_both_real_memory_call_sites_use_the_caption(enrich):
    prompt = enrich(REQUEST)
    agent = _turn_agent()
    _memory_turn_start_and_prefetch(agent, prompt)
    agent._memory_manager.prefetch_all.assert_called_once_with(REQUEST, session_id="s-1")
    agent = _post_turn_agent(display_kind=None)
    agent._sync_external_memory_for_turn(original_user_message=prompt, final_response="Answer.", interrupted=False)
    agent._memory_manager.queue_prefetch_all.assert_called_once_with(REQUEST, session_id="s-1")
    assert agent._memory_manager.sync_all.call_args.args[0] == prompt
