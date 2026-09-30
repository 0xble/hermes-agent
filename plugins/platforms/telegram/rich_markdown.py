"""Normalization for Telegram's permissive Rich Markdown block parser."""

import re


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


# Same literal-region skipping as above, then a footnote reference directly
# followed by another reference (not a ``[^n]:`` definition).
_ADJACENT_FOOTNOTE_RE = re.compile(
    r"(?P<fence>`{3,}|~{3,})[^\n]*\n.*?(?:(?P=fence)(?![`~])|\Z)"
    r"|(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)"
    r"|\$\$.*?(?:\$\$|\Z)"
    r"|(?P<ref>\[\^[^\]\s]+\])(?=\[\^[^\]\s]+\](?!:))",
    re.DOTALL,
)
FOOTNOTE_SEPARATOR = "<sup>,</sup>"


def separate_adjacent_footnote_refs(text: str) -> str:
    """Render ``[^2][^3]`` as superscript "2,3" instead of "23".

    Telegram merges adjacent footnote references into one superscript run
    with no separator, so two citations read as a single larger number. A
    superscript comma joins the same run and keeps each reference tappable.
    """
    def replace(match: re.Match[str]) -> str:
        ref = match.group("ref")
        return match.group(0) if ref is None else ref + FOOTNOTE_SEPARATOR

    return _ADJACENT_FOOTNOTE_RE.sub(replace, text)


_LIST_LINE_RE = re.compile(r"^(?:[ \t]*(?:[-+*]|\d+[.)])[ \t]+|[ \t]+\S)")
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^[^\]\s]+\]:")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
LIST_FOOTNOTE_SPACER = "<!-- -->"


def separate_footnote_defs_from_lists(text: str) -> str:
    """Keep the first footnote definition after a list.

    Telegram's parser drops the first ``[^n]:`` definition when it directly
    follows a list, even across blank lines, leaving a literal ``^n`` in the
    list. An empty HTML comment on the line before the definitions ends the
    list without rendering a block of its own.
    """
    lines = text.split("\n")
    out: list[str] = []
    fence = ""
    previous = ""
    for line in lines:
        opener = _FENCE_RE.match(line)
        if fence:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = ""
        elif opener:
            fence = opener.group(1)
        elif _FOOTNOTE_DEF_RE.match(line) and _LIST_LINE_RE.match(previous):
            if out and out[-1].strip():
                out.append("")
            out.append(LIST_FOOTNOTE_SPACER)
        out.append(line)
        if line.strip():
            previous = line
    return "\n".join(out)
