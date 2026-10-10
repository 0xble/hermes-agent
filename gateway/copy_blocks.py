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

    def __init__(self, *, drop_bodies: bool = False) -> None:
        # drop_bodies=True removes whole blocks (markers and bodies) for surfaces whose
        # copy blocks are delivered separately; False keeps bodies inline.
        self._drop_bodies = drop_bodies
        self._in_copy = False
        self._pending = ""  # held line prefix that may still become a marker line
        self._line = ""  # already-seen text of the current line, for fence tracking
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
        # Inside a block an open marker is body text, as in extract_copy_blocks.
        return self._fence is None and (
            (_line_marker(line, _COPY_OPEN) and not self._in_copy) or _line_marker(line, _COPY_CLOSE)
        )

    def _on_marker(self, line: str) -> None:
        if _line_marker(line, _COPY_OPEN) and not self._in_copy:
            self._in_copy = True
        elif _line_marker(line, _COPY_CLOSE) and self._in_copy:
            self._in_copy = False
        # A nested open or stray close is control syntax and changes nothing.

    def _emit(self) -> bool:
        return not (self._drop_bodies and self._in_copy)

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
                    if self._emit():
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
                self._on_marker(segment)  # a complete marker line is control syntax
            else:
                if self._emit():
                    output.append(segment)
                self._fence = _next_fence(self._fence, full_line)
            self._line = ""
            cursor = end
        return "".join(output)

    def flush(self) -> str:
        """Release a non-marker tail, or drop it when it is a complete marker line."""
        pending, self._pending = self._pending, ""
        self._line = ""
        if not pending or self._is_marker(pending) or not self._emit():
            return ""
        return pending


# Platforms whose adapter sends a ``copy_block`` payload byte-exact as plain text. Every other
# platform formats outbound text, so its copy blocks stay inline in the ordinary reply instead.
PLAIN_COPY_PLATFORMS = frozenset({"telegram"})


def platform_sends_copy_blocks(platform) -> bool:
    return str(getattr(platform, "value", platform) or "").lower() in PLAIN_COPY_PLATFORMS


def adapter_sends_copy_blocks(adapter) -> bool:
    return platform_sends_copy_blocks(getattr(adapter, "platform", None))


def split_copy_blocks_for(adapter, text: str) -> tuple[str, list[str]]:
    """Separate copy blocks where *adapter* can send them plain, else render them inline."""
    if adapter_sends_copy_blocks(adapter):
        return extract_copy_blocks(text)
    return render_copy_blocks_inline(text), []


def copy_free_text_for(adapter, text: str) -> str:
    """The streamed body *adapter* shows: bodies removed where they go out separately."""
    return strip_copy_blocks(text) if adapter_sends_copy_blocks(adapter) else render_copy_blocks_inline(text)


def strip_copy_blocks(text: str) -> str:
    """Return *text* without copy blocks, for display surfaces that send them separately."""
    return extract_copy_blocks(text)[0]


def map_outside_copy_blocks(text: str, transform, *, keep_markers: bool = False) -> str:
    """Apply *transform* only to text outside copy blocks, then render blocks inline.

    Directive processing such as MEDIA resolution must not rewrite paste-ready bodies,
    so each run of ordinary text is transformed on its own and bodies stay byte-exact.
    With ``keep_markers`` the marker lines stay, for callers that stream the result
    through :class:`CopyMarkerStreamFilter`.
    """
    if not text or (_COPY_OPEN not in text and _COPY_CLOSE not in text):
        return transform(text) if text else text
    out: list[str] = []
    ordinary: list[str] = []
    fence: str | None = None
    in_copy = False

    def _flush_ordinary() -> None:
        if ordinary:
            out.append(transform("".join(ordinary)))
            ordinary.clear()

    for line in text.splitlines(keepends=True):
        if fence is None and (
                (_line_marker(line, _COPY_OPEN) and not in_copy) or _line_marker(line, _COPY_CLOSE)):
            _flush_ordinary()
            in_copy = _line_marker(line, _COPY_OPEN)
            if keep_markers:
                out.append(line)
            continue
        (out if in_copy else ordinary).append(line)
        fence = _next_fence(fence, line)
    _flush_ordinary()
    return "".join(out)


def render_copy_blocks_inline(text: str) -> str:
    """Render copy blocks inline for response surfaces that cannot send separate messages.

    Marker lines are removed and each body stays where it was, exactly as
    :class:`CopyMarkerStreamFilter` renders the same text when streamed, so a
    surface's streamed and final text match. Callers apply this only to their output,
    never to persisted transcript text.
    """
    if not text or (_COPY_OPEN not in text and _COPY_CLOSE not in text):
        return text
    stream = CopyMarkerStreamFilter()
    return stream.feed(text) + stream.flush()


__all__ = [
    "CopyMarkerStreamFilter",
    "extract_copy_blocks",
    "map_outside_copy_blocks",
    "render_copy_blocks_inline",
    "strip_copy_blocks",
]
