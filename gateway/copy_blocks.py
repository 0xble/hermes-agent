"""Pure extraction of native ``[[copy]]`` response blocks."""

from __future__ import annotations

import re

_COPY_OPEN = "[[copy]]"
_COPY_CLOSE = "[[/copy]]"
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _line_marker(line: str, marker: str) -> bool:
    """Match a marker line while accepting only surrounding whitespace."""
    return line.strip() == marker


def _next_fence(fence: str | None, line: str) -> str | None:
    """Return the open fence after *line*, following CommonMark fence rules.

    A fence closes only on a run of the same character at least as long as the
    opener with nothing else on the line, so a longer fence can quote shorter ones.
    """
    match = _FENCE_RE.match(line.rstrip("\r\n"))
    if match is None:
        return fence
    run, rest = match.group(1), match.group(2)
    if fence is None:
        if run[0] == "`" and "`" in rest:
            return None  # an info string may not contain backticks
        return run
    if run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
        return None
    return fence


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
        if fence is None and _line_marker(line, _COPY_OPEN if not in_copy else _COPY_CLOSE):
            if in_copy:
                body = "".join(copy_parts)
                body = _strip_one_adjacent_newline(body, leading=True)
                body = _strip_one_adjacent_newline(body, leading=False)
                if body.strip():
                    blocks.append(body)
                copy_parts = []
            in_copy = not in_copy
            continue
        if fence is None and not in_copy and _line_marker(line, _COPY_CLOSE):
            # A stray close marker is control syntax, never visible text.
            continue
        # Fences are tracked inside copy bodies too, so a fenced marker line is
        # body text there, matching CopyMarkerStreamFilter.
        (copy_parts if in_copy else remaining).append(line)
        fence = _next_fence(fence, line)

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
        self._pending = ""  # held line prefix that may still become a marker line
        self._line = ""  # already-emitted text of the current line, for fence tracking
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

    def _is_marker(self, line: str) -> bool:
        return self._fence is None and (
            _line_marker(line, _COPY_OPEN) or _line_marker(line, _COPY_CLOSE)
        )

    def feed(self, delta: str) -> str:
        """Filter one streamed text delta, holding only a possible trailing marker line."""
        if not delta:
            return ""
        text = self._pending + delta
        self._pending = ""
        output: list[str] = []
        cursor = 0
        while cursor < len(text):
            match = re.search(r"\r\n|\n|\r", text[cursor:])
            if match is None:
                tail = text[cursor:]
                if self._fence is None and not self._line and self._could_be_marker_prefix(tail):
                    self._pending = tail
                else:
                    output.append(tail)
                    self._line += tail
                break
            end = cursor + match.end()
            if text[end - 1] == "\r" and end == len(text):
                # A lone CR may be the first half of CRLF; wait for the next delta.
                self._pending = text[cursor:]
                break
            segment = text[cursor:end]
            full_line = self._line + segment
            if not self._line and self._is_marker(segment):
                pass  # a complete marker line is control syntax
            else:
                output.append(segment)
                self._fence = _next_fence(self._fence, full_line)
            self._line = ""
            cursor = end
        return "".join(output)

    def flush(self) -> str:
        """Release a non-marker tail, or drop it when it is a complete marker line."""
        pending, self._pending = self._pending, ""
        self._line = ""
        if not pending or self._is_marker(pending):
            return ""
        return pending


def copy_preview_text(text: str, *, final: bool) -> str:
    """Return editable-preview text with copy blocks and marker syntax hidden.

    Block bodies are removed because they are delivered as separate messages. On an
    interim preview, a trailing partial line that may still become a marker line is
    held back so an edit never shows half a marker.
    """
    remaining, _ = extract_copy_blocks(text)
    if final:
        return remaining
    return CopyMarkerStreamFilter().feed(remaining)


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


__all__ = [
    "CopyMarkerStreamFilter",
    "copy_preview_text",
    "extract_copy_blocks",
    "render_copy_blocks_inline",
]
