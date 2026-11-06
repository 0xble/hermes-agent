"""Copy context must not downgrade native rich constructs (regression in #1149)."""
from html import unescape
import re
from unittest.mock import AsyncMock

from markdown_it import MarkdownIt
import pytest

from gateway.copy_blocks import copy_free_text_for, extract_copy_blocks
from plugins.platforms.telegram.rich_delivery import rich_message_payload
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


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['send', 'draft', 'edit'])
@pytest.mark.parametrize('streamed', [False, True])
@pytest.mark.parametrize('nested', [False, True])
async def test_markdown_labels_after_copy_quotes_are_not_raw_html(operation, streamed, nested):
    # Real preview shape: the first label rendered bold, but labels after quotes
    # became literal **text** when a visual spacer consumed the HTML blank line.
    adapter = _make_adapter({'rich_messages': 'always', 'rich_drafts': True})
    labels = ['1. Budget comment', '2. Date comment', '3. Longer prompt', 'This week']
    bodies = ['Who approves the budget?', 'Is the first test Friday or Monday?', LONG]
    source = '\n\n'.join(
        f'**{label}**\n[[copy]]\n{body}\n[[/copy]]'
        for label, body in zip(labels, bodies)
    ) + f'\n\n**{labels[-1]}**\nNext.'
    if nested:
        source = f'<details><summary>Context</summary>\n\n{source}\n\n</details>'
    content = copy_free_text_for(adapter, source) if streamed else source
    if operation == 'send':
        result = await adapter.send('12345', content)
    elif operation == 'draft':
        result = await adapter.send_draft('12345', 123, content)
    else:
        result = await adapter.edit_message('12345', '123', content, finalize=True)
    assert result.success
    assert adapter._bot is not None
    markdown = adapter._bot.do_api_request.call_args.kwargs['api_kwargs']['rich_message']['markdown']
    # Telegram uses GFM, except that it explicitly parses Markdown within details.
    # Parse that body independently to exercise GFM's raw-HTML termination rule.
    if nested:
        markdown = markdown.removeprefix('<details><summary>Context</summary>')
        markdown = markdown.removesuffix('</details>')
    rendered = MarkdownIt('commonmark', {'html': True}).render(markdown)
    for label in labels:
        assert f'<strong>{label}</strong>' in rendered
    for label in labels[1:]:
        assert re.search(r'</blockquote>\n{2,}\*\*' + re.escape(label) + r'\*\*', markdown)
    assert [(bool(expandable), _quote_text(body)) for expandable, body in re.findall(
        r'<blockquote( expandable)?>(.*?)</blockquote>', markdown, re.DOTALL
    )] == [(False, bodies[0]), (False, bodies[1]), (True, bodies[2])]
    adapter._bot.send_message.assert_not_called()


@pytest.mark.parametrize(('quote_kind', 'gap'), [
    ('copy', '\n'), ('copy', '\n\n'), ('html', '\n\n'), ('html-leading-break', '\n\n'),
])
@pytest.mark.parametrize('line_count', [6, 7])
@pytest.mark.parametrize(('following', 'rendered_block'), [
    ('**Next label**', '<strong>Next label</strong>'),
    ('[Next link](https://example.com)', '<a href="https://example.com">Next link</a>'),
    ('7. Next item\n8. Another item', '<ol start="7">'),
    ('## Next heading', '<h2>Next heading</h2>'),
    ('| Case | Status |\n|---|---|\n| next | yes |', '<table>'),
    ('```python\nprint("next")\n```', '<pre><code class="language-python">'),
    ('> Next quote', '<blockquote>\n<p>Next quote</p>'),
])
def test_rich_quote_boundaries_reopen_following_markdown(quote_kind, gap, line_count, following, rendered_block):
    body = '\n'.join(f'line {index}' for index in range(line_count))
    if quote_kind == 'copy':
        quote = f'[[copy]]\n{body}\n[[/copy]]'
    else:
        expandable = ' expandable' if line_count > 6 else ''
        leading = '\n' if quote_kind == 'html-leading-break' else ''
        quote = f'<blockquote{expandable}>{leading}{body}</blockquote>'
    markdown = rich_message_payload('**Before**' + gap + quote + gap + following)['markdown']
    assert re.search(r'</blockquote>\n{2,}', markdown)
    rendered = MarkdownIt('commonmark', {'html': True}).enable('table').render(markdown)
    assert '<strong>Before</strong>' in rendered
    assert rendered_block in rendered
    assert '\u00a0' not in markdown  # structural boundaries are not visual prose spacers
