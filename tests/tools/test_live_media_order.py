"""Local adapter delivery preserves descriptor order, captions and file URI semantics."""
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest

from tools.send_message_tool import _send_live_adapter_media
from gateway.platforms.base import SendResult


class Adapter:
    def __init__(self):
        self.sent = []
        self.text = []

    async def send(self, *, content, **kwargs):
        self.text.append(content)
        return SimpleNamespace(success=True, message_id='text')

    async def send_video(self, chat, path, **kwargs):
        self.sent.append((path, kwargs.get('caption')))
        return SimpleNamespace(success=True, message_id='video')

    async def send_image_file(self, chat, path, **kwargs):
        self.sent.append((path, kwargs.get('caption')))
        return SimpleNamespace(success=True, message_id='image')

    async def send_multiple_images(self, *, images, **kwargs):
        self.sent.extend(images)
        return [SimpleNamespace(success=True, message_id=str(i)) for i in range(len(images))]


@pytest.mark.asyncio
async def test_mixed_media_retains_original_order_and_caption(tmp_path):
    files = [tmp_path / name for name in ['first.mp4', 'second.png', 'third.png']]
    for path in files:
        path.touch()
    adapter = Adapter()
    result = await _send_live_adapter_media(adapter, 'chat', 'caption', [(str(p), False) for p in files])
    assert result.get('success'), result
    assert adapter.sent == [(str(path), None) for path in files]
    assert adapter.text == ['caption']


@pytest.mark.asyncio
async def test_relative_image_album_uses_local_escaped_file_uris(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = [Path('one space.png'), Path('two#hash.png')]
    for path in files:
        path.touch()
    adapter = Adapter()
    result = await _send_live_adapter_media(adapter, 'chat', 'caption', [(str(p), False) for p in files])
    assert result.get('success'), result
    for (uri, _caption), path in zip(adapter.sent, files):
        parsed = urlparse(uri)
        assert parsed.scheme == 'file' and not parsed.netloc
        assert Path(unquote(parsed.path)) == path.resolve()
    assert [caption for _uri, caption in adapter.sent] == [None, '']
    assert adapter.text == ['caption']


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,complete,confirmed", [
    ("none", False, 0), ("empty", False, 0), ("short", False, 1),
    ("null_item", False, 1), ("missing_success", False, 1),
    ("extra", False, 0), ("complete", True, 2),
    ("aggregate", True, 2), ("aggregate_failure", False, 0),
])
async def test_album_requires_complete_receipts_without_resending(tmp_path, kind, complete, confirmed):
    first = SendResult(success=True, message_id="first")
    second = SendResult(success=True, message_id="second")
    receipts = {"none": None, "empty": [], "short": [first],
                "null_item": [first, None], "missing_success": [first, SimpleNamespace(message_id="unknown")],
                "extra": [first, second, first], "complete": [first, second],
                "aggregate": SendResult(success=True, message_id="album"),
                "aggregate_failure": SendResult(success=False, error="unconfirmed album")}

    class AlbumAdapter(Adapter):
        async def send_multiple_images(self, **kwargs):
            self.sent.append("album-attempt")
            return receipts[kind]

    files = [tmp_path / "one.png", tmp_path / "two.png"]
    for path in files:
        path.touch()
    adapter = AlbumAdapter()
    result = await _send_live_adapter_media(adapter, "chat", "separate text", [(str(p), False) for p in files])
    assert bool(result.get("success")) is complete
    assert result["_media_delivered"] == confirmed
    assert adapter.sent == ["album-attempt"]
    assert adapter.text == ["separate text"]
    if not complete:
        assert result.get("error")
        assert result["_text_message_id"] == "text"
