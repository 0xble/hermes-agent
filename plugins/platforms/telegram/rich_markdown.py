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
# Within one paragraph: inline code and display math (possibly multiline) are
# skipped; a reference followed by another reference gets the separator.
_ADJACENT_REF_RE = re.compile(
    r"(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)"
    r"|\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)"
    rf"|(?P<ref>{_FOOTNOTE_REF})(?={_FOOTNOTE_REF}(?!:))",
    re.DOTALL,
)
_FOOTNOTE_DEF_RE = re.compile(rf"^{_FOOTNOTE_REF}:")
_PARSER = MarkdownIt("commonmark")
_LITERAL_BLOCKS = frozenset({"fence", "code_block", "html_block"})
_LIST_OPENS = frozenset({"bullet_list_open", "ordered_list_open"})
FOOTNOTE_SEPARATOR = "<sup>,</sup>"
LIST_FOOTNOTE_SPACER = "<!-- -->"


def _separate_refs(block: str) -> str:
    def replace(match: re.Match[str]) -> str:
        ref = match.group("ref")
        return match.group(0) if ref is None else ref + FOOTNOTE_SEPARATOR

    return _ADJACENT_REF_RE.sub(replace, block)


def normalize_footnotes(text: str) -> str:
    """Work around two Telegram rich-parser footnote defects.

    Adjacent references (``[^2][^3]``) merge into one superscript run that
    reads "23"; a superscript comma between them renders "2,3" with each
    number still tappable. The first ``[^n]:`` definition directly after a
    list is dropped, even across blank lines, leaving a literal ``^n``; an
    empty HTML comment before the definitions ends the list without adding a
    block. A CommonMark parse decides which lines are prose, code, or list, so
    code blocks, inline code, and display math stay literal.
    """
    if "[^" not in text:
        return text
    lines = text.split("\n")
    literal: set[int] = set()
    lists: list[tuple[int, int]] = []
    for token in _PARSER.parse(text):
        if not token.map:
            continue
        first, end = token.map
        if token.type in _LITERAL_BLOCKS:
            literal.update(range(first, end))
        elif token.type in _LIST_OPENS:
            lists.append((first, end))
        elif token.type == "inline":
            separated = _separate_refs("\n".join(lines[first:end])).split("\n")
            if len(separated) == end - first:
                lines[first:end] = separated

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
