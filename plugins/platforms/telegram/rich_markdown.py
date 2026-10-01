"""Normalization for Telegram's permissive Rich Markdown block parser."""

import re

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
    return False


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
_PARSER.inline.ruler.before("link", "telegram_adjacent_footnote", _adjacent_ref_rule)


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
