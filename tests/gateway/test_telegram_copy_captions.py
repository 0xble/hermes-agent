"""Display-only copy sentinels never cross Telegram's media caption boundary."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from gateway.copy_blocks import copy_free_text_for
from gateway.platforms.base import utf16_len
from tests.gateway.test_telegram_voice_caption_markdown import _make_adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["formatted", "overflow", "parse-rejection", "format-error", "audio", "document"])
async def test_auto_tts_copy_caption_fallbacks_keep_body_visible_but_never_spoken(monkeypatch, tmp_path, variant):
    import plugins.platforms.telegram.adapter as tg
    import tools.tts_tool as tts

    adapter = _make_adapter()
    adapter._bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=3))
    monkeypatch.setattr(tg, "_probe_voice_duration_seconds", lambda _p: 3)
    extension, method = {"audio": ("mp3", "send_audio"), "document": ("flac", "send_document")}.get(
        variant, ("ogg", "send_voice"))
    path = tmp_path / f"reply.{extension}"
    path.write_bytes(b"audio")
    spoken = []

    def synthesize(*, text, output_path):
        spoken.append(text)
        return json.dumps({"success": True, "file_path": str(path)})

    monkeypatch.setattr(tts, "check_tts_requirements", lambda: True)
    monkeypatch.setattr(tts, "text_to_speech_tool", synthesize)
    body = "*" * 650 if variant == "overflow" else "*literal* `code` &amp;"
    raw = f"Before.\n[[copy]]\n{body}\n[[/copy]]\nAfter."
    event = SimpleNamespace(source=SimpleNamespace(chat_id="123", platform="telegram"))
    extracted = await adapter._extract_response_content(raw, event, "", is_ephemeral_response=True)
    assert "\x00HERMES_COPY_OPEN\x00" in extracted.text_content
    assert len(extracted.text_content) <= 1024
    if variant == "overflow":
        assert utf16_len(adapter.format_message(extracted.text_content)) > 1024
    transport = getattr(adapter._bot, method)
    if variant == "parse-rejection":
        transport.side_effect = [BadRequest("can't parse entities"), SimpleNamespace(message_id=7)]
    elif variant == "format-error":
        monkeypatch.setattr(adapter, "format_message", lambda _: (_ for _ in ()).throw(ValueError("formatter failed")))

    paths, _ = await adapter._synthesize_auto_tts(adapter._spoken_reply_text(extracted))
    assert paths == [str(path)]
    assert spoken == ["Before. After."]
    deliveries = []
    assert await adapter._play_tts_file(event, extracted.text_content, paths[0], True, {}, deliveries.append)
    assert deliveries[-1].success
    wires = [call.kwargs for call in transport.await_args_list]
    assert all("\x00" not in wire["caption"] and "HERMES_COPY_" not in wire["caption"] for wire in wires)
    assert all(utf16_len(wire["caption"]) <= 1024 for wire in wires)
    if variant == "formatted":
        assert wires[0]["parse_mode"] == "MarkdownV2"
        assert wires[0]["caption"] == "Before\\.\n> \\*literal\\* \\`code\\` &amp;\nAfter\\."
    else:
        assert wires[-1].get("parse_mode") is None
        assert wires[-1]["caption"] == f"Before.\n{body}\nAfter."
    if variant == "parse-rejection":
        assert len(wires) == 2 and wires[0]["parse_mode"] == "MarkdownV2"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["document", "photo", "image-file", "video", "animation", "album"])
@pytest.mark.parametrize("internal", [False, True])
async def test_plain_media_captions_render_markers_before_truncating(monkeypatch, tmp_path, operation, internal):
    import plugins.platforms.telegram.adapter as tg
    import telegram

    monkeypatch.setattr(telegram, "InputMediaPhoto", lambda **kw: SimpleNamespace(**kw))
    adapter = _make_adapter()
    for method in ["send_document", "send_photo", "send_video", "send_animation", "send_media_group"]:
        setattr(adapter._bot, method, AsyncMock(return_value=SimpleNamespace(message_id=4)))
    adapter._bot.send_media_group.return_value = [SimpleNamespace(message_id=4)]
    monkeypatch.setattr(tg, "_probe_video_geometry", lambda _p: {})
    path = tmp_path / "media.bin"
    path.write_bytes(b"media")
    # The old [:1024] could bisect a closing sentinel, so stripping must happen first.
    body = "*literal* " + "x" * 980
    raw = f"Before\n[[copy]]\n{body}\n[[/copy]]\n" + "After" * 30
    caption = copy_free_text_for(adapter, raw) if internal else raw
    if operation == "album":
        result = await adapter.send_multiple_images("123", [("https://example.com/image.png", caption)])
        wire = adapter._bot.send_media_group.await_args.kwargs["media"][0].caption
    elif operation == "photo":
        result = await adapter.send_image("123", "https://example.com/image.png", caption=caption)
        wire = adapter._bot.send_photo.await_args.kwargs["caption"]
    elif operation == "animation":
        result = await adapter.send_animation("123", "https://example.com/image.gif", caption=caption)
        wire = adapter._bot.send_animation.await_args.kwargs["caption"]
    else:
        fn, parameter, transport = {
            "document": (adapter.send_document, "file_path", "send_document"),
            "image-file": (adapter.send_image_file, "image_path", "send_photo"),
            "video": (adapter.send_video, "video_path", "send_video"),
        }[operation]
        result = await fn("123", **{parameter: str(path)}, caption=caption)
        wire = getattr(adapter._bot, transport).await_args.kwargs["caption"]
    assert result.success
    assert wire == (f"Before\n{body}\n" + "After" * 30)[:1024]
    assert "\x00" not in wire and "HERMES_COPY_" not in wire
