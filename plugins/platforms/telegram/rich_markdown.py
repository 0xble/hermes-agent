"""Normalization for Telegram's permissive Rich Markdown block parser."""

import re
from html import escape as html_escape

from markdown_it import MarkdownIt


# Consume code/math before looking for block prefixes so their literal contents
# survive unchanged, including an unfinished code fence in a streaming draft.
_LITERAL_HASH_RE = re.compile(
    r"(?P<fence>`{3,}|~{3,})[^\n]*\n.*?(?:(?P=fence)(?![`~])|\Z)"
    r"|(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)"
    r"|\$\$.*?(?:\$\$|\Z)"
    r"|(?P<prefix>^[ \t]*(?:(?:>[ \t]*|[-+*][ \t]+|\d+[.)][ \t]+))*)"
    r"(?P<hashes>#+)(?=[^\s#])",
    re.MULTILINE | re.DOTALL,
)


def escape_literal_hash_prefixes(text: str) -> str:
    """Keep ``#89``/``#tag`` as prose, including in lists and blockquotes.

    Telegram accepts these as headings even without the whitespace required by
    standard Markdown. Escape only such block-start hashes, not real headings,
    inline references, links, code, or already escaped prefixes.
    """
    def replace(match: re.Match[str]) -> str:
        hashes = match.group("hashes")
        if hashes is None:
            return match.group(0)
        return match.group("prefix") + r"\#" * len(hashes)

    return _LITERAL_HASH_RE.sub(replace, text)


_FOOTNOTE_REF = r"\[\^[^\]\s]+\]"
_ADJACENT_REF_RE = re.compile(rf"{_FOOTNOTE_REF}(?={_FOOTNOTE_REF}(?!:))")
_FOOTNOTE_DEF_RE = re.compile(rf"^{_FOOTNOTE_REF}:")
_MATH_DELIMITERS = (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"))
# Closed single-dollar math, as the adapter's currency guard (``_RICH_CURRENCY_PROTECTED_RE``) keeps
# it: ``$5 and refs ... $6`` is currency, not math, so references between the amounts stay prose.
_INLINE_DOLLAR_MATH_RE = re.compile(
    r"(?<![\\$])\$(?![\s$])"
    r"(?!\d+(?:,\d{3})*(?:\.\d+)?\s+[A-Za-z]{2,}\b)"
    r"[^$\n]*?(?<![\s([{,:;])\$(?![\w$])"
)
# Inline HTML whose content Telegram shows literally; formatting tags such as <b> still wrap prose.
_LITERAL_HTML_RE = re.compile(r"<(code|kbd|samp|pre)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_REFS_KEY = "telegram_adjacent_footnote_ends"
_LITERAL_BLOCKS = frozenset({"fence", "code_block", "html_block"})
_LIST_OPENS = frozenset({"bullet_list_open", "ordered_list_open"})
FOOTNOTE_SEPARATOR = "<sup>,</sup>"
LIST_FOOTNOTE_SPACER = "<!-- -->"


def _math_rule(state, silent: bool) -> bool:
    """Consume display/inline TeX that Telegram renders, so references inside stay literal."""
    for opener, closer in _MATH_DELIMITERS:
        if state.src.startswith(opener, state.pos):
            close = state.src.find(closer, state.pos + len(opener))
            if close < 0 or close + len(closer) > state.posMax:
                return False
            if not silent:
                state.push("text", "", 0).content = state.src[state.pos:close + len(closer)]
            state.pos = close + len(closer)
            return True
    return _literal_span(state, silent, _INLINE_DOLLAR_MATH_RE)


def _literal_html_rule(state, silent: bool) -> bool:
    """Consume a literal inline HTML container (``<code>…</code>``) whole."""
    return _literal_span(state, silent, _LITERAL_HTML_RE)


def _literal_span(state, silent: bool, pattern: "re.Pattern[str]") -> bool:
    match = pattern.match(state.src, state.pos)
    if not match or match.end() > state.posMax:
        return False
    if not silent:
        state.push("text", "", 0).content = match.group(0)
    state.pos = match.end()
    return True


def _adjacent_ref_rule(state, silent: bool) -> bool:
    """Record a reference directly followed by another reference.

    It runs only where markdown-it reaches prose, never inside code spans,
    escapes, links, autolinks or inline HTML, which earlier rules consume.
    """
    match = _ADJACENT_REF_RE.match(state.src, state.pos)
    if not match or match.end() > state.posMax:
        return False
    if not silent:
        ends = state.env.get(_REFS_KEY)
        if ends is not None:
            ends.add(match.end())
        state.push("text", "", 0).content = match.group(0)
    state.pos = match.end()
    return True


_PARSER = MarkdownIt("commonmark")
_PARSER.inline.ruler.before("escape", "telegram_math", _math_rule)
_PARSER.inline.ruler.before("html_inline", "telegram_literal_html", _literal_html_rule)
_PARSER.inline.ruler.before("link", "telegram_adjacent_footnote", _adjacent_ref_rule)

# Telegram's Rich Markdown parser treats an HTML ``blockquote`` as a raw block,
# so Markdown inside an expandable blockquote is displayed literally. Render
# only the inline Markdown in those blocks as HTML, which Telegram does parse
# there, while leaving the native quote and expandable behavior intact.
_BLOCKQUOTE_RE = re.compile(
    r"(?P<open><blockquote\b[^>]*>)(?P<body>[\s\S]*?)(?P<close></blockquote\s*>)",
    re.IGNORECASE,
)
_BLOCKQUOTE_PARSER = MarkdownIt("commonmark", {"html": True}).enable(
    "strikethrough"
)
_BLOCKQUOTE_FENCE_RE = re.compile(
    r"^[ \t]*(?P<marker>`{3,}|~{3,})[^\n]*\n"
    r"(?:[\s\S]*?^[ \t]*(?P=marker)[ \t]*(?:\n|\Z)|[\s\S]*\Z)",
    re.MULTILINE,
)
_BLOCKQUOTE_CODE_SPAN_RE = re.compile(
    r"(?P<ticks>`+)(?!`)(?P<body>.*?)(?<!`)(?P=ticks)(?!`)",
    re.DOTALL,
)


def _protect_blockquote_code(body: str) -> tuple[str, dict[str, str]]:
    """Protect code regions while the surrounding Markdown is rendered."""
    protected: dict[str, str] = {}

    def stash(value: str) -> str:
        key = f"\ue000{len(protected)}\ue001"
        protected[key] = value
        return key

    body = _BLOCKQUOTE_FENCE_RE.sub(lambda match: stash(match.group(0)), body)
    body = _LITERAL_HTML_RE.sub(lambda match: stash(match.group(0)), body)

    def stash_code_span(match: re.Match[str]) -> str:
        code = match.group("body")
        if len(code) >= 2 and code[0] == code[-1] == " ":
            code = code[1:-1]
        return stash(f"<code>{html_escape(code, quote=False)}</code>")

    body = _BLOCKQUOTE_CODE_SPAN_RE.sub(stash_code_span, body)
    return body, protected


def _restore_blockquote_code(text: str, protected: dict[str, str]) -> str:
    for key, value in protected.items():
        text = text.replace(key, value)
    return text


def _render_blockquote(match: re.Match[str]) -> str:
    raw_body = match.group("body")
    body = raw_body.strip("\r\n")
    if not body:
        return match.group(0)
    leading = raw_body[: len(raw_body) - len(raw_body.lstrip("\r\n"))]
    trailing = raw_body[len(raw_body.rstrip("\r\n")) :]
    body, protected = _protect_blockquote_code(body)
    rendered_lines = [
        _BLOCKQUOTE_PARSER.renderInline(line)
        .replace("<strong>", "<b>")
        .replace("</strong>", "</b>")
        .replace("<em>", "<i>")
        .replace("</em>", "</i>")
        for line in body.splitlines()
    ]
    rendered = (
        f"{match.group('open')}{leading}{'<br>'.join(rendered_lines)}"
        f"{trailing}{match.group('close')}"
    )
    return _restore_blockquote_code(rendered, protected)


def normalize_blockquote_markdown(text: str) -> str:
    """Render inline Markdown inside HTML blockquotes as Telegram HTML.

    Rich Markdown does not parse Markdown nested inside ``<blockquote>`` or
    ``<blockquote expandable>``. Telegram does parse supported HTML tags there,
    so convert each line's inline Markdown and preserve line breaks as ``<br>``.
    Only actual HTML blockquote tokens are changed, leaving fenced examples and
    inline code byte-for-byte unchanged.
    """
    if "<blockquote" not in text.lower():
        return text

    lines = text.splitlines(keepends=True)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))

    replacements: list[tuple[int, int, str]] = []
    open_line: int | None = None
    for token in _BLOCKQUOTE_PARSER.parse(text):
        if token.type != "html_block" or not token.map:
            continue
        if open_line is None and re.search(r"<blockquote\b", token.content, re.IGNORECASE):
            open_line = token.map[0]
        if open_line is not None and re.search(
            r"</blockquote\s*>", token.content, re.IGNORECASE
        ):
            start, end = line_starts[open_line], line_starts[token.map[1]]
            original = text[start:end]
            rendered = _BLOCKQUOTE_RE.sub(_render_blockquote, original)
            if rendered != original:
                replacements.append((start, end, rendered))
            open_line = None

    if not replacements:
        return text
    out: list[str] = []
    cursor = 0
    for start, end, rendered in replacements:
        out.append(text[cursor:start])
        out.append(rendered)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _separate_inline(lines: list[str], first: int, content: str, env: dict) -> None:
    """Insert separators in ``lines`` at the reference ends found in one inline block."""
    ends: set[int] = set()
    _PARSER.parseInline(content, {**env, _REFS_KEY: ends})
    if not ends:
        return
    content_lines = content.split("\n")
    offsets: list[int] = []
    for index, content_line in enumerate(content_lines):
        raw = lines[first + index].rstrip() if first + index < len(lines) else ""
        stripped = content_line.rstrip()
        if not raw.endswith(stripped):
            return  # block prefix/suffix not recoverable; leave it untouched
        offsets.append(len(raw) - len(stripped))
    line_start = 0
    for index, content_line in enumerate(content_lines):
        line_end = line_start + len(content_line)
        columns = sorted((end - line_start for end in ends if line_start < end <= line_end), reverse=True)
        raw = lines[first + index]
        for column in columns:
            at = offsets[index] + column
            raw = raw[:at] + FOOTNOTE_SEPARATOR + raw[at:]
        lines[first + index] = raw
        line_start = line_end + 1


def normalize_footnotes(text: str) -> str:
    """Work around two Telegram rich-parser footnote defects.

    Adjacent references (``[^2][^3]``) merge into one superscript run that
    reads "23"; a superscript comma between them renders "2,3" with each
    number still tappable. The first ``[^n]:`` definition directly after a
    list is dropped, even across blank lines, leaving a literal ``^n``; an
    empty HTML comment before the definitions ends the list without adding a
    block. markdown-it decides what is prose, so code, math, links, autolinks,
    inline HTML and escapes stay literal.
    """
    if "[^" not in text:
        return text
    lines = text.split("\n")
    literal: set[int] = set()
    lists: list[tuple[int, int]] = []
    env: dict = {}
    for token in _PARSER.parse(text, env):
        if not token.map:
            continue
        first, end = token.map
        if token.type in _LITERAL_BLOCKS:
            literal.update(range(first, end))
        elif token.type in _LIST_OPENS:
            lists.append((first, end))
        elif token.type == "inline":
            _separate_inline(lines, first, token.content, env)

    out: list[str] = []
    previous = -1
    for index, line in enumerate(lines):
        if (
            index not in literal
            and _FOOTNOTE_DEF_RE.match(line)
            and previous >= 0
            and not _FOOTNOTE_DEF_RE.match(lines[previous])
            and any(first <= previous < end for first, end in lists)
        ):
            if out and out[-1].strip():
                out.append("")
            out.append(LIST_FOOTNOTE_SPACER)
        out.append(line)
        if line.strip():
            previous = index
    return "\n".join(out)
