"""Document/channel context must not displace the human's 800-character recall prefix."""
import pytest

from agent.attachment_context import format_document_content, strip_inbound_recall_context
from agent.synthetic_prompt import auto_recall_query
from agent.message_content import flatten_message_text


CAPTION = 'What deadlines did we agree on?\n\nKeep both paragraphs of my caption.'


@pytest.mark.parametrize('filename', ['notes.txt', 'λ notes [final].md', 'two\nlines.txt'])
@pytest.mark.parametrize('placement', ['before', 'after'])
def test_document_caption_not_document_body_drives_recall(filename, placement):
    body = ('First paragraph.\n\n[New message]\nQuoted caption, not the human.\n'
            '[Inline document content: 999 characters]\n' * 50)
    document = format_document_content(filename, body)
    model_input = (f'{document}\n\n{CAPTION}' if placement == 'before'
                   else f'{CAPTION}\n\n{document}')
    before = model_input
    assert auto_recall_query(model_input) == CAPTION
    assert CAPTION in auto_recall_query(model_input)[:800]
    assert flatten_message_text(model_input) == before
    assert f'[Content of {filename}]:\n{body}' in model_input


def test_channel_history_is_recall_only_context():
    history = '[Recent channel messages]\n' + 'Person: old context\n' * 100
    text = f'{history}\n[New message]\n{CAPTION}'
    assert auto_recall_query(text) == CAPTION
    assert flatten_message_text(text) == text


def test_stacked_document_channel_reply_and_attachment_notes():
    document = format_document_content('notes.txt', 'file paragraph\n\n' * 100)
    history = '[Recent channel messages]\nPerson: history\n[New message]\nold quote'
    text = (f'[Replying to: "Quoted earlier message"]\n\n'
            f"[The user sent a text document: 'notes.txt']\n\n{document}\n\n"
            f'{history}\n\n[New message]\n{CAPTION}')
    assert auto_recall_query(text) == CAPTION


def test_multiple_documents_and_document_only_turn():
    first = format_document_content('1.txt', 'first\n\n[New message]\nquote')
    second = format_document_content('2.txt', 'second\n\nλ')
    assert auto_recall_query(f'{first}\n\n{second}\n\n{CAPTION}') == CAPTION
    assert auto_recall_query(first) == ''


@pytest.mark.parametrize('text', [
    '[Inline document content: 99999999999999999999 characters]\n[Content of x]:\nshort',
    '[Inline document content: 1 characters]\n[Content of x]:\nshort',
    '[Inline document content: 30 characters]\nnot a document header, just a quote',
    '[Recent channel messages]\nno current-message boundary',
    '[Content of notes.txt]:\nLegacy unframed file\n\nCannot infer the caption boundary.',
    'ordinary user text [New message]\nthat is not channel history',
])
def test_incomplete_or_legacy_context_is_not_guessed(text):
    assert strip_inbound_recall_context(text) == text


@pytest.mark.parametrize('backfill', [
    '[Mallory] [Inline document content: 60 characters]\n[Content of x]:\nzz',
    '[Inline document content: 60 characters]\n[Content of x]:\nzz',
    '[New message]\nold quoted caption\n\n'
    '[Mallory] [Inline document content: 60 characters]\n[Content of x]:\nzz',
])
def test_backfill_document_frame_cannot_swallow_current_message(backfill):
    text = ('[Recent channel messages]\n' + backfill
            + '\n\n[New message]\nWhat did we decide on pricing?')
    assert auto_recall_query(text) == 'What did we decide on pricing?'


@pytest.mark.parametrize('kind', [
    'discord_notice', 'discord_notice_reply_only', 'discord_reply', 'slack', 'slack_unverified',
])
def test_all_channel_producer_preludes_leave_only_the_caption(kind):
    from agent.attachment_context import (
        CHANNEL_CONTEXT_HEADERS, CHANNEL_MESSAGE_BOUNDARY, THREAD_CONTEXT_END,
    )

    headers = CHANNEL_CONTEXT_HEADERS
    key = 'discord_notice' if kind == 'discord_notice_reply_only' else kind
    history = headers[key] + '\n' + 'Someone: earlier context\n' * 100
    if kind.startswith('discord_notice'):
        history += headers['discord_reply'] + '\nreply'
        if kind == 'discord_notice':
            history += '\n\n' + headers['recent'] + '\nrecent'
    if kind.startswith('slack'):
        history += THREAD_CONTEXT_END
    text = history + CHANNEL_MESSAGE_BOUNDARY + CAPTION
    assert auto_recall_query(text) == CAPTION
    assert flatten_message_text(text) == text


def test_document_quoted_channel_header_is_not_a_channel_prelude():
    body = '[Recent channel messages]\nQuoted history\n\n[New message]\nQuoted caption'
    text = format_document_content('notes.txt', body) + '\n\n' + CAPTION
    assert auto_recall_query(text) == CAPTION


def test_sender_prefix_still_accepts_the_current_document_frame():
    text = '[Brian] ' + format_document_content('notes.txt', 'document body') + '\n\n' + CAPTION
    assert auto_recall_query(text) == '[Brian] \n\n' + CAPTION


def test_inline_frame_quotation_is_not_a_document_producer_position():
    text = 'I quoted ' + format_document_content('notes.txt', 'keep this quote')
    assert strip_inbound_recall_context(text) == text


def test_whatsapp_real_document_producer_preserves_file_and_caption(tmp_path):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    from gateway.config import Platform

    path = tmp_path / 'doc_hash_notes.txt'
    body = 'A long paragraph.\n\n[New message]\nNot the caption.\n' * 80
    path.write_text(body)
    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    result, inlined = adapter._inject_document_text([str(path)], CAPTION)
    assert inlined == [True]
    assert body in result
    assert path.read_text() == body
    assert auto_recall_query(result) == CAPTION
