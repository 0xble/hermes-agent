"""Counted document frames survive the actual adapter media-batching seams."""
import importlib
from unittest.mock import Mock

import pytest

from agent.attachment_context import format_document_content
from agent.synthetic_prompt import auto_recall_query
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['feishu', 'telegram', 'slack', 'discord', 'whatsapp'])
@pytest.mark.parametrize('caption', ['', 'What deadlines did we agree on?'])
async def test_document_frames_survive_adapter_media_batch(platform, caption):
    module = importlib.import_module(f'plugins.platforms.{platform}.adapter')
    name = {'feishu': 'FeishuAdapter', 'telegram': 'TelegramAdapter',
            'slack': 'SlackAdapter', 'discord': 'DiscordAdapter', 'whatsapp': 'WhatsAppAdapter'}[platform]
    cls = getattr(module, name)
    source = SessionSource(platform=Platform(platform), chat_id='chat', chat_type='dm', user_id='user')
    frames = [format_document_content(name + '.txt', body) for name, body in [('a', 'a\n'), ('b', 'b\n')]]
    events = [MessageEvent(text=frame, source=source, message_type=MessageType.DOCUMENT,
                           media_urls=[f'/cache/{i}.txt'], media_types=['text/plain'],
                           media_text_inlined=[True]) for i, frame in enumerate(frames)]
    if caption:
        events[0].text = caption + '\n\n' + events[0].text
    if platform == 'feishu':
        adapter = cls(PlatformConfig())
        adapter._schedule_media_batch_flush = Mock()
        for event in events:
            await adapter._enqueue_media_event(event)
        merged = next(iter(adapter._pending_media_batches.values()))
    elif platform == 'telegram':
        adapter = object.__new__(cls)
        pending = {}
        for event in events:
            adapter._merge_into_pending(pending, 'album', event)
        merged = pending['album']
    else:
        # These adapters inherit the base busy-session media batching path.
        adapter = object.__new__(cls)
        adapter.platform = Platform(platform)
        adapter._pending_messages = {}
        adapter._busy_session_handler = None
        adapter._shared_runtime_owns_input = lambda event: False
        adapter._canonicalize = lambda source: None
        adapter.gateway_runner = None
        for event in events:
            event.allow_gateway_control = False
            await adapter._handle_message_while_active(event, 'session')
        merged = adapter._pending_messages['session']
    assert merged.media_urls == ['/cache/0.txt', '/cache/1.txt']
    assert auto_recall_query(merged.text) == caption
    assert all(frame in merged.text for frame in frames)
