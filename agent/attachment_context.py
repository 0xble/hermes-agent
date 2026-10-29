"""Producer framing for inline documents and recall-only inbound context extraction."""
from __future__ import annotations

import re

_DOCUMENT_FRAME = re.compile(r"\[Inline document content: ([0-9]{1,20}) characters\]\n")
_CHANNEL_HEADER = "[Recent channel messages]\n"
_CHANNEL_BOUNDARY = "\n[New message]\n"


def format_document_content(display_name: str, content: str) -> str:
    """Keep file text verbatim, with an unambiguous boundary before a human caption.

    Paragraph breaks and bracketed markers can occur inside a document, so they
    cannot delimit the caption. The count covers the complete existing injection,
    including the display name, and uses Python characters rather than bytes.
    """
    block = f"[Content of {display_name}]:\n{content}"
    return f"[Inline document content: {len(block)} characters]\n{block}"


def strip_inbound_recall_context(text: str) -> str:
    """Remove only complete producer frames, leaving model input and retention alone."""
    parts: list[str] = []
    position = 0
    for match in _DOCUMENT_FRAME.finditer(text):
        if match.start() < position:
            continue
        size = int(match[1])
        end = match.end() + size
        if (size < len('[Content of ]:\n') or end > len(text)
                or not text.startswith('[Content of ', match.end())):
            continue
        parts.append(text[position:match.start()])
        position = end
    if parts:
        parts.append(text[position:])
        text = ''.join(parts).strip()
    # The last boundary wins, just like generated-turn boundaries: a copy in
    # backfill cannot make channel history look like the current person's text.
    start = text.find(_CHANNEL_HEADER)
    if start >= 0:
        boundary = text.rfind(_CHANNEL_BOUNDARY, start + len(_CHANNEL_HEADER))
        if boundary >= 0:
            text = (text[:start] + text[boundary + len(_CHANNEL_BOUNDARY):]).strip()
    return text
