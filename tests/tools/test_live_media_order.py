"""Local adapter delivery preserves descriptor order, captions and file URI semantics."""
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest

from tools.send_message_tool import _send_live_adapter_media


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
