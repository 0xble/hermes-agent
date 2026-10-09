"""Pure extraction of native ``[[copy]]`` response blocks."""

from __future__ import annotations

import re

_COPY_OPEN = "[[copy]]"
_COPY_CLOSE = "[[/copy]]"
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def _line_marker(line: str, marker: str) -> bool:
    """Match a marker line while accepting only surrounding whitespace."""
    return line.strip() == marker


def _fence_kind(line: str) -> str | None:
    match = _FENCE_RE.match(line)
    return match.group(1)[:3] if match else None


def _strip_one_adjacent_newline(text: str, *, leading: bool) -> str:
    """Remove one line ending, preserving all other bytes exactly."""
    if leading:
        if text.startswith("\r\n"):
            return text[2:]
        if text.startswith(("\n", "\r")):
            return text[1:]
    else:
        if text.endswith("\r\n"):
            return text[:-2]
        if text.endswith(("\n", "\r")):
            return text[:-1]
    return text


def extract_copy_blocks(text: str) -> tuple[str, list[str]]:
    """Extract explicit copy blocks from *text*.

    Marker lines are recognized outside fenced code blocks. The returned text has all
    marker lines and block bodies removed, while each block body is preserved byte-for-byte
    apart from one newline adjacent to each marker. The operation is idempotent because the
    returned text contains no active marker lines.
    """
    if not text or ("[[copy]]" not in text and "[[/copy]]" not in text):
        return text, []

    lines = text.splitlines(keepends=True)
    remaining: list[str] = []
    blocks: list[str] = []
    in_copy = False
    copy_parts: list[str] = []
    fence: str | None = None

    for line in lines:
        if in_copy:
            if _line_marker(line, _COPY_CLOSE):
                body = "".join(copy_parts)
                body = _strip_one_adjacent_newline(body, leading=True)
                body = _strip_one_adjacent_newline(body, leading=False)
                if body.strip():
                    blocks.append(body)
                in_copy = False
                copy_parts = []
            else:
                copy_parts.append(line)
            continue

        if fence is None and _line_marker(line, _COPY_OPEN):
            in_copy = True
            copy_parts = []
            continue
        if fence is None and _line_marker(line, _COPY_CLOSE):
            # A stray close marker is control syntax, never visible text.
            continue

        remaining.append(line)
        if fence is None:
            candidate = _fence_kind(line)
            if candidate is not None:
                fence = candidate
        else:
            candidate = _fence_kind(line)
            if candidate == fence:
                fence = None

    if in_copy:
        body = _strip_one_adjacent_newline("".join(copy_parts), leading=True)
        if body.strip():
            blocks.append(body)

    return "".join(remaining), blocks


__all__ = ["extract_copy_blocks"]
