"""Literal inline context for separately delivered paste-ready blocks."""
from collections.abc import Callable
from html import escape as html_escape
import re

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


def protect_rich_copy_quotes(content: str) -> tuple[str, dict[str, str]]:
    """Hide literal bodies until all rich Markdown shaping is finished.

    Rich Markdown does not parse Markdown inside HTML blockquotes, but does
    parse HTML tags/entities. Escape those once, then restore after the normal
    blockquote Markdown-to-HTML converter so it cannot interpret the body.
    """
    text, bodies = protect_inline_copy_bodies(content)
    quotes = {}
    for token, body in bodies.items():
        body = _strip_one_adjacent_newline(body, leading=True)
        body = _strip_one_adjacent_newline(body, leading=False)
        lines = body.splitlines() or [""]
        key = f"HERMESRICHCOPY{len(quotes)}END"
        while key in content:
            key += "X"
        expandable = " expandable" if len(lines) > 6 else ""
        # Keep the placeholder inside an HTML block even within a details body.
        # A Markdown quote placeholder would be rewritten there before restore.
        text = text.replace(token, f"<blockquote{expandable}>{key}</blockquote>\n\n")
        quotes[key] = "<br>".join(html_escape(line, quote=False) for line in lines)
    return text, quotes


def protect_quote_fallback(content: str) -> tuple[str, dict[str, str]]:
    """Unescape quoted bodies without interpreting their literal Markdown on parse fallback."""
    bodies = {}

    def stash(match):
        key = f"HERMESCOPYFALLBACK{len(bodies)}END"
        while key in content:
            key += "X"
        quote = match.group(0).removeprefix("**")
        quote = re.sub(r"(?<!\\)\|\|$", "", quote)
        bodies[key] = re.sub(r"\\([_*\[\]()~`>#\+\-=|{}.!\\])", r"\1", quote)
        return key

    return re.sub(r"(?m)^(?:\*\*)?> [^\n]*(?:\n> [^\n]*)*", stash, content), bodies


def truncate_copy_source(content: str, max_length: int, len_fn, indicator_overhead: int, *, close_chunks: bool = False):
    """Split source, not encoded quotes: each API message gets a balanced quote.

    The conservative half-budget leaves room for worst-case MarkdownV2 escaping,
    a reopened display marker, expandable wrappers and the normal chunk indicator.
    Literal body fences are shielded from the ordinary source fence balancer.
    """
    from gateway.copy_blocks import (
        CopyMarkerStreamFilter, _has_copy_markers, balance_copy_quote_chunks,
        restore_inline_copy_bodies, copy_quote_continuation, _DISPLAY_COPY_CLOSE,
    )
    from gateway.platforms.base import BasePlatformAdapter

    if not _has_copy_markers(content):
        return None
    if len_fn(content) * 2 + 8 <= max_length:
        return [content]
    if "\x00HERMES_COPY_" not in content:
        stream = CopyMarkerStreamFilter(keep_markers=True)
        content = stream.feed(content) + stream.flush()
    protected, bodies = protect_inline_copy_bodies(content, keep_markers=True)
    shields = (chr(n) for n in range(0xE000, 0xF900) if chr(n) not in content)
    tick, tilde = next(shields), next(shields)
    shielded = {key: body.replace("`", tick).replace("~", tilde) for key, body in bodies.items()}
    content = restore_inline_copy_bodies(protected, shielded)
    chunks = BasePlatformAdapter.truncate_message(
        content, max(64, max_length // 2 - 64), len_fn=len_fn, indicator_overhead=indicator_overhead)
    parts = [re.sub(r" \(\d+/\d+\)$", "", chunk) for chunk in chunks]
    result = []
    for index, chunk in enumerate(balance_copy_quote_chunks(parts), 1):
        if close_chunks and copy_quote_continuation(chunk):
            chunk += "\n" + _DISPLAY_COPY_CLOSE
        if len(parts) > 1:
            chunk += f"\n ({index}/{len(parts)})"
        result.append(chunk.replace(tick, "`").replace(tilde, "~"))
    return result
