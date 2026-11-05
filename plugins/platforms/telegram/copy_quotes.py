"""Literal inline context for separately delivered paste-ready blocks."""
from collections.abc import Callable

from gateway.copy_blocks import protect_inline_copy_bodies, _strip_one_adjacent_newline


def protect_copy_quotes(content: str, stash: Callable[[str], str], escape: Callable[[str], str]) -> str:
    """Stash quotes before Markdown conversion so body syntax is never interpreted.

    Fences are ordinary copied text, not nested pre entities. MarkdownV2's
    empty bold prefix and final || make a >6-line quote expandable.
    """
    text, bodies = protect_inline_copy_bodies(content)
    for token, body in bodies.items():
        newline = "\n" if body.endswith(("\n", "\r")) else ""
        body = _strip_one_adjacent_newline(body, leading=True)
        body = _strip_one_adjacent_newline(body, leading=False)
        lines = body.splitlines() or [""]
        quote = "\n".join("> " + escape(line) for line in lines)
        if len(lines) > 6:
            quote = "**" + quote + "||"
        # A token occupies the complete body, including the newline before the close.
        text = text.replace(token, stash(quote) + newline)
    return text
