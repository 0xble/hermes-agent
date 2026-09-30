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


_FOOTNOTE_REF = r"\[\^[^\]\s]+\]"
# Inline literals are skipped; a reference followed by another reference (not a
# ``[^n]:`` definition) gets the separator.
_ADJACENT_REF_RE = re.compile(
    r"(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)"
    r"|\\\[.*?\\\]|\\\(.*?\\\)"
    rf"|(?P<ref>{_FOOTNOTE_REF})(?={_FOOTNOTE_REF}(?!:))"
)
_FOOTNOTE_DEF_RE = re.compile(rf"^{_FOOTNOTE_REF}:")
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])(?:[ \t]|$)")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)")
FOOTNOTE_SEPARATOR = "<sup>,</sup>"
LIST_FOOTNOTE_SPACER = "<!-- -->"


def _separate_refs(line: str) -> str:
    def replace(match: re.Match[str]) -> str:
        ref = match.group("ref")
        return match.group(0) if ref is None else ref + FOOTNOTE_SEPARATOR

    return _ADJACENT_REF_RE.sub(replace, line)


def _display_math_end(stripped: str) -> str:
    """Closing delimiter for display math opened on this line and left open, else ''."""
    for opener, closer in (("$$", "$$"), ("\\[", "\\]")):
        if stripped.startswith(opener):
            return "" if closer in stripped[len(opener):] else closer
    return ""


def normalize_footnotes(text: str) -> str:
    """Work around two Telegram rich-parser footnote defects.

    Adjacent references (``[^2][^3]``) merge into one superscript run that
    reads "23"; a superscript comma between them renders "2,3" with each
    number still tappable. The first ``[^n]:`` definition directly after a
    list is dropped, even across blank lines, leaving a literal ``^n``; an
    empty HTML comment before the definitions ends the list without adding a
    block. Code (fenced, indented, inline) and display math stay literal.
    """
    out: list[str] = []
    fence = math_end = ""
    in_list = in_code = False
    previous_blank = True
    for line in text.split("\n"):
        stripped = line.strip()
        opener = _FENCE_RE.match(line)
        if fence:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = ""
        elif math_end:
            if math_end in stripped:
                math_end = ""
        elif opener:
            fence = opener.group(1)
        elif stripped.startswith(("$$", "\\[")):
            math_end = _display_math_end(stripped)
        elif stripped and _INDENTED_CODE_RE.match(line) and not in_list and (previous_blank or in_code):
            in_code = True
        elif stripped:
            in_code = False
            if _LIST_ITEM_RE.match(line):
                in_list = True
            elif _FOOTNOTE_DEF_RE.match(line):
                if in_list:
                    if out and out[-1].strip():
                        out.append("")
                    out.append(LIST_FOOTNOTE_SPACER)
                in_list = False
            elif previous_blank and not line[:1].isspace():
                in_list = False
            line = _separate_refs(line)
        previous_blank = not stripped
        out.append(line)
    return "\n".join(out)
