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


class CopyMarkerStreamFilter:
    """Hide copy marker lines from a streamed single-response surface.

    Text is emitted immediately except for a trailing line prefix that can still become an
    exact marker line. Marker recognition follows :func:`extract_copy_blocks`: only lines
    outside a fenced code block are control syntax, and CRLF is preserved for ordinary text.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._line_start = True
        self._fence: str | None = None

    @staticmethod
    def _could_be_marker_prefix(line: str) -> bool:
        stripped = line.lstrip()
        if not stripped:
            return True
        for marker in (_COPY_OPEN, _COPY_CLOSE):
            if marker.startswith(stripped):
                return True
            if stripped.startswith(marker) and stripped[len(marker):].strip() == "":
                return True
        return False

    def _process_line(self, line: str) -> str:
        body = line.rstrip("\r\n")
        if self._fence is None and _line_marker(body, _COPY_OPEN):
            return ""
        if self._fence is None and _line_marker(body, _COPY_CLOSE):
            return ""
        if self._fence is None:
            candidate = _fence_kind(body)
            if candidate is not None:
                self._fence = candidate
        else:
            candidate = _fence_kind(body)
            if candidate == self._fence:
                self._fence = None
        return line

    def feed(self, delta: str) -> str:
        """Filter one streamed text delta, holding only a possible trailing marker line."""
        if not delta:
            return ""
        text = self._pending + delta
        self._pending = ""
        output: list[str] = []
        line_start = self._line_start
        cursor = 0
        while cursor < len(text):
            match = re.search(r"\r\n|\n|\r", text[cursor:])
            if match is None:
                tail = text[cursor:]
                if self._fence is None and line_start and self._could_be_marker_prefix(tail):
                    self._pending = tail
                else:
                    output.append(tail)
                    line_start = False
                break
            end = cursor + match.end()
            if text[cursor:end].endswith(chr(13)) and end == len(text):
                self._pending = text[cursor:]
                line_start = True
                break
            line = text[cursor:end]
            output.append(self._process_line(line))
            cursor = end
            line_start = True
        self._line_start = line_start
        return "".join(output)

    def flush(self) -> str:
        """Release a non-marker tail, or drop it when it is a complete marker line."""
        if not self._pending:
            return ""
        pending, self._pending = self._pending, ""
        if self._fence is None and _line_marker(pending, _COPY_OPEN):
            return ""
        if self._fence is None and _line_marker(pending, _COPY_CLOSE):
            return ""
        return pending


def render_copy_blocks_inline(text: str) -> str:
    """Render copy blocks inline for response surfaces that cannot send separate messages.

    Marker lines are control syntax on those surfaces, so retain each block body in source
    order and separate it from the ordinary response with a blank line. This helper does
    not mutate persisted transcript text; callers should apply it only to their output.
    """
    remaining, blocks = extract_copy_blocks(text)
    if not blocks:
        return remaining
    inline_parts = (remaining.rstrip("\r\n"), *blocks)
    return "\n\n".join(part for part in inline_parts if part)


__all__ = ["CopyMarkerStreamFilter", "extract_copy_blocks", "render_copy_blocks_inline"]
