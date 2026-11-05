"""Copy context must not downgrade native rich constructs (regression in #1149)."""
from html import unescape
import re
from unittest.mock import AsyncMock

import pytest

from gateway.copy_blocks import copy_free_text_for, extract_copy_blocks
from tests.gateway.test_telegram_rich_messages import _make_adapter


SHORT = '*literal* `code` [link](https://example.com) [^1][^2] &amp; &#36; HERMESRICHCOPY0END'
LONG = '\n'.join([
    '# literal heading', '> literal quote', '- [x] literal task',
    '<details><summary>literal</summary>$$x$$</details>',
    '```markdown', '[[copy]]', '</blockquote><b>not bold</b>', '```',
])
REPLY = (
    'Evidence.[^1][^2]\n\n'
    '<details><summary>Context</summary>\n\nNative toggle.\n\n</details>\n\n'
    '| Case | Result |\n|---|---|\n| rich | yes |\n\n'
    f'Before short.\n[[copy]]\n{SHORT}\n[[/copy]]\nBetween.\n'
    f'[[copy]]\n{LONG}\n[[/copy]]\nAfter long.\n\n'
    '[^1]: First source\n[^2]: Second source'
)


def _quote_text(body):
    return unescape(body.replace('<br>', '\n'))


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['send', 'draft', 'edit'])
@pytest.mark.parametrize('streamed', [False, True])
@pytest.mark.parametrize('nested', [False, True])
async def test_copy_quotes_preserve_native_rich_constructs(operation, streamed, nested):
    adapter = _make_adapter({'rich_messages': 'always', 'rich_drafts': True})
    source = f'<details><summary>All</summary>\n\n{REPLY}\n\n</details>' if nested else REPLY
    content = copy_free_text_for(adapter, source) if streamed else source
    if operation == 'send':
        result = await adapter.send('12345', content)
    elif operation == 'draft':
        result = await adapter.send_draft('12345', 123, content)
    else:
        result = await adapter.edit_message('12345', '123', content, finalize=True)
    assert result.success
    call = adapter._bot.do_api_request.call_args
    assert call.args[0] == {
        'send': 'sendRichMessage', 'draft': 'sendRichMessageDraft', 'edit': 'editMessageText',
    }[operation]
    md = call.kwargs['api_kwargs']['rich_message']['markdown']
    quotes = re.findall(r'<blockquote( expandable)?>(.*?)</blockquote>', md, re.DOTALL)
    assert [(bool(expandable), _quote_text(body)) for expandable, body in quotes] == [
        (False, SHORT), (True, LONG),
    ]
    assert 'Evidence.[^1]<sup>,</sup>[^2]' in md
    assert '<details><summary>Context</summary>' in md and 'Native toggle.' in md
    assert '| Case | Result |\n|---|---|\n| rich | yes |' in md
    assert '[^1]: First source' in md and '[^2]: Second source' in md
    assert md.index('Before short.') < md.index('<blockquote>') < md.index('Between.')
    assert md.index('Between.') < md.index('<blockquote expandable>') < md.index('After long.')
    assert '\x00' not in md
    assert '&lt;/blockquote&gt;&lt;b&gt;not bold&lt;/b&gt;' in md
    assert '&amp;amp; &amp;#36;' in md  # authored entities are literal, decoded only once
    adapter._bot.send_message.assert_not_called()
    adapter._bot.send_message_draft.assert_not_called()
    adapter._bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['send', 'draft', 'edit'])
@pytest.mark.parametrize('rejection', ['unsupported', 'oversized', 'disabled'])
@pytest.mark.parametrize('streamed', [False, True])
async def test_rich_copy_fallback_keeps_literal_quotes_and_separate_copy_bytes(operation, rejection, streamed):
    from telegram.error import BadRequest

    adapter = _make_adapter({'rich_messages': 'always', 'rich_drafts': True})
    adapter._retrigger_typing = AsyncMock()
    adapter._bot.do_api_request.side_effect = BadRequest('rich content unsupported')
    if rejection == 'oversized':
        # Source fits this budget, but HTML escaping makes the actual payload exceed it.
        adapter.RICH_MESSAGE_MAX_CHARS = len(REPLY) + 1
    elif rejection == 'disabled':
        adapter._rich_messages_enabled = False
    content = copy_free_text_for(adapter, REPLY) if streamed else REPLY
    if operation == 'send':
        result = await adapter.send('12345', content)
        legacy = adapter._bot.send_message
    elif operation == 'draft':
        result = await adapter.send_draft('12345', 123, content)
        legacy = adapter._bot.send_message_draft
    else:
        result = await adapter.edit_message('12345', '123', content, finalize=True)
        legacy = adapter._bot.edit_message_text
    assert result.success
    assert adapter._bot.do_api_request.call_count == (1 if rejection == 'unsupported' else 0)
    wire = legacy.call_args.kwargs
    assert wire['parse_mode'] == 'MarkdownV2'
    assert '\x00' not in wire['text']
    assert '> \\*literal\\* \\`code\\`' in wire['text']
    assert '**> \\# literal heading' in wire['text'] and '||' in wire['text']
    assert '<blockquote' not in wire['text']
    _, blocks = extract_copy_blocks(REPLY)
    assert blocks == [SHORT, LONG]
    adapter._bot.send_message.reset_mock()
    adapter._chat_outbound_slot_remaining = lambda _chat: 0
    for body in blocks:
        assert (await adapter.send('12345', body, metadata={'copy_block': True, 'plain': True})).success
        copy_wire = adapter._bot.send_message.call_args.kwargs
        assert copy_wire['text'] == body and copy_wire['parse_mode'] is None
