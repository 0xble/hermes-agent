"""Shared Bot API Rich Message shaping and safe-fallback classification."""
from typing import Any


def rich_message_payload(content: str, *, skip_entity_detection: bool = False) -> dict[str, Any]:
    """Build ``InputRichMessage`` from raw Markdown, never MarkdownV2-escaped text.

    Literal hash references, footnotes, currency and linebreaks retain the live
    adapter's shaping rules across the gateway and standalone CLI/cron lanes.
    """
    from .adapter import _degrade_unsupported_markdown_links, _protect_rich_currency, _rich_normalize_linebreaks
    from .copy_quotes import protect_rich_copy_quotes
    from gateway.copy_blocks import restore_inline_copy_bodies
    from .rich_markdown import (
        escape_literal_hash_prefixes,
        normalize_blockquote_markdown,
        normalize_footnotes,
    )

    content, quotes = protect_rich_copy_quotes(content)
    markdown = normalize_blockquote_markdown(
        normalize_footnotes(
            escape_literal_hash_prefixes(_degrade_unsupported_markdown_links(content))
        )
    )
    markdown = _rich_normalize_linebreaks(_protect_rich_currency(markdown))
    payload: dict[str, Any] = {"markdown": restore_inline_copy_bodies(markdown, quotes)}
    if skip_entity_detection:
        payload["skip_entity_detection"] = True
    return payload


def is_rich_capability_error(exc: Exception) -> bool:
    """True when the endpoint itself is unavailable, not a per-message refusal."""
    if exc.__class__.__name__.lower() in {"endpointnotfound", "invalidtoken"}:
        return True
    if isinstance(exc, (AttributeError, TypeError, NotImplementedError)) or getattr(exc, "error_code", None) == 404:
        return True
    s = str(exc).lower()
    if ("method" in s or "endpoint" in s) and ("not found" in s or "does not exist" in s):
        return True
    return "no such method" in s


def is_rich_fallback_error(exc: Exception) -> bool:
    """Only clearly permanent refusals are safe to legacy-resend (duplicate risk)."""
    from .adapter import TelegramAdapter

    if TelegramAdapter._is_bad_request_error(exc) or is_rich_capability_error(exc):
        return True
    s = str(exc).lower()
    return "unsupported" in s or "not implemented" in s
