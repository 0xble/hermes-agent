"""Provider error context for credential-pool status: parsing and privacy."""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from agent.credential_pool_timestamps import _parse_absolute_timestamp
from agent.retry_utils import reset_delay_from_message

_ERROR_REASON_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _withhold_private_error_text(updates: dict[str, Any]) -> dict[str, Any]:
    """Provider error text can echo the prompt. While private, keep only classifier fields."""
    if not {"last_error_message", "last_error_reason"} & updates.keys():
        return updates
    from agent.incognito import is_incognito
    if not is_incognito():
        return updates
    cleaned = dict(updates)
    if cleaned.get("last_error_message") is not None:
        cleaned["last_error_message"] = None
    reason = cleaned.get("last_error_reason")
    if isinstance(reason, str) and not _ERROR_REASON_TOKEN.fullmatch(reason):
        cleaned["last_error_reason"] = None
    return cleaned


def _normalize_error_context(error_context: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(error_context, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in ("reason", "message"):
        value = error_context.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    reset_at = (
        error_context.get("reset_at")
        or error_context.get("resets_at")
        or error_context.get("retry_until")
    )
    parsed_reset_at = _parse_absolute_timestamp(reset_at)
    message = error_context.get("message")
    if parsed_reset_at is None and isinstance(message, str):
        retry_delay_seconds = reset_delay_from_message(message)
        if retry_delay_seconds is not None:
            parsed_reset_at = time.time() + retry_delay_seconds
    if parsed_reset_at is not None:
        normalized["reset_at"] = parsed_reset_at
    return normalized
