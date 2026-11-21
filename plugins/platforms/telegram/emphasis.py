"""Asterisk emphasis for the legacy Telegram MarkdownV2 converter."""

from collections.abc import Callable


def protect_asterisk_emphasis(
    text: str, protect: Callable[[str], str], escape: Callable[[str], str],
    *, block_source: str,
) -> str:
    """Resolve emphasis with the renderer's shared delimiter pairing rules.

    The legacy MarkdownIt adapter duplicated delimiter handling and treated mark
    runs inside a stashed bare URL as ordinary prose.  That made URL punctuation
    consume emphasis delimiters before restoration.  Keep the block-source
    parameter for the caller's compatibility, but use the canonical pairer that
    also drives the URL emphasis scan.
    """
    from .telegram_markdown import _resolve_emphasis
    return _resolve_emphasis(text, protect, escape)
