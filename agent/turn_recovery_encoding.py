"""Encoding and image-payload repair before provider error classification."""

from __future__ import annotations

from typing import Any


def _runtime_uses_ascii_encoding() -> bool:
    """Return whether the process genuinely needs an ASCII-only request fallback."""
    from agent.turn_recovery import locale
    encoding = locale.getpreferredencoding(False).strip().lower().replace("_", "-")
    return encoding in {"ascii", "us-ascii", "ansi-x3.4-1968"}


def _repair_transport_credentials(agent: Any) -> bool:
    """Strip non-ASCII from ``_client_kwargs["default_headers"]`` and the API key.

    Non-ASCII in the key makes httpx fail encoding the Authorization header — the usual
    persistent cause of UnicodeEncodeError that survives message/tool sanitization (#6843,
    e.g. ʋ instead of v from a bad copy-paste). Entra ID bearer providers are callables
    minting ASCII JWTs; skip them (``_strip_non_ascii`` would crash). Returns True when
    either the headers or the key were repaired.
    """
    from agent.turn_recovery import _sanitize_structure_non_ascii, _strip_non_ascii, _vlines
    _client_kwargs = getattr(agent, "_client_kwargs", None)
    _default_headers = _client_kwargs.get("default_headers") if isinstance(_client_kwargs, dict) else None
    _repaired = bool(isinstance(_default_headers, dict) and _sanitize_structure_non_ascii(_default_headers))
    _raw_key = getattr(agent, "api_key", None) or ""
    if isinstance(_raw_key, str) and _raw_key:
        _clean_key = _strip_non_ascii(_raw_key)
        if _clean_key != _raw_key:
            agent.api_key = _clean_key
            if isinstance(_client_kwargs, dict):
                _client_kwargs["api_key"] = _clean_key
            # The live client reads its own api_key copy on every request.
            if getattr(agent, "client", None) is not None and hasattr(agent.client, "api_key"):
                agent.client.api_key = _clean_key
            _repaired = True
            _vlines(
                agent,
                "⚠️  API key contained non-ASCII characters (bad copy-paste?) — stripped them. "
                "If auth fails, re-copy the key from your provider's dashboard.",
            )
    return _repaired


def _recover_unicode_encode_error(
    agent: Any, api_error: Exception, messages: list[dict[str, Any]], api_messages: Any,
    api_kwargs: Any, active_system_prompt: Any,
) -> tuple[bool, Any]:
    """UnicodeEncodeError recovery: lone surrogates (clipboard paste) first, then an ASCII
    codec under a non-UTF-8 locale. Sanitizes in place; bounded by the caller's
    ``_unicode_sanitization_passes < 2`` guard (surrogate strip, then ASCII-only)."""
    from agent.turn_recovery import (
        _repair_transport_credentials, _runtime_uses_ascii_encoding, _sanitize_messages_non_ascii,
        _sanitize_messages_surrogates, _sanitize_structure_surrogates, _strip_non_ascii, _vlines,
    )
    _err_str = str(api_error).lower()
    _is_ascii_codec = "'ascii'" in _err_str or "ascii" in _err_str
    # utf-8 refusing U+D800..U+DFFF ("surrogates not allowed").
    _is_surrogate_error = "surrogate" in _err_str or ("'utf-8'" in _err_str and not _is_ascii_codec)
    # Sanitize canonical messages for surrogate recovery, but keep ASCII recovery
    # request-local: API copies may carry fields absent from the durable transcript.
    _surrogates_found = _sanitize_messages_surrogates(messages)
    _surrogates_found |= isinstance(api_messages, list) and _sanitize_messages_surrogates(api_messages)
    _surrogates_found |= isinstance(api_kwargs, dict) and _sanitize_structure_surrogates(api_kwargs)
    # Gate the retry on the error type, not on whether anything was found — a new
    # transformed field could slip through.
    if _surrogates_found or _is_surrogate_error:
        if _surrogates_found:
            # In-place rewrites may have popped _DB_PERSISTED_MARKER off stamped live dicts;
            # force a full flush scan so the repaired rows are rewritten.
            agent._db_flush_scan_prefix = None
        agent._unicode_sanitization_passes += 1
        agent._buffer_vprint(
            "⚠️  Stripped invalid surrogate characters from messages. Retrying..."
            if _surrogates_found else
            "⚠️  Surrogate encoding error — retrying after full-payload sanitization..."
        )
        return True, active_system_prompt
    if not _is_ascii_codec:
        return False, active_system_prompt

    # Error text is provider-controlled and can mention ``ascii`` even when the
    # process sends UTF-8. In that normal case, do not rewrite conversation,
    # tools, prompts, or prefill; only repair values that can poison an ASCII
    # transport header. If nothing was repaired, an identical retry cannot
    # succeed — return False so the error surfaces through the normal path
    # instead of burning both sanitization passes on unchanged requests.
    if not _runtime_uses_ascii_encoding():
        if not _repair_transport_credentials(agent):
            return False, active_system_prompt
        agent._unicode_sanitization_passes += 1
        _vlines(
            agent,
            "⚠️  Repaired non-ASCII request credentials/headers without changing conversation content. Retrying...",
        )
        return True, active_system_prompt

    agent._force_ascii_payload = True
    # Strip all non-ASCII from the request-local api_messages (reused across retries). The
    # failed attempt's api_kwargs is NOT touched: build_api_request rebuilds it from
    # ``agent.tools`` on the next iteration and ``sanitize_outbound_kwargs`` strips the whole
    # payload under ``_force_ascii_payload``. Canonical agent state stays byte-stable.
    _messages_sanitized = isinstance(api_messages, list) and _sanitize_messages_non_ascii(api_messages)

    _system_sanitized = False
    if isinstance(active_system_prompt, str):
        _sanitized_system = _strip_non_ascii(active_system_prompt)
        if _sanitized_system != active_system_prompt:
            active_system_prompt = _sanitized_system
            _system_sanitized = True

    _transport_repaired = _repair_transport_credentials(agent)

    # Always retry on ASCII codec detection: _force_ascii_payload sanitizes the full
    # api_kwargs next iteration even when the checks above find nothing.
    agent._unicode_sanitization_passes += 1
    _vlines(
        agent,
        "⚠️  System encoding is ASCII — stripped non-ASCII characters from request payload. Retrying..."
        if (_messages_sanitized or _system_sanitized or _transport_repaired) else
        "⚠️  System encoding is ASCII — enabling full-payload sanitization for retry...",
    )
    return True, active_system_prompt


def _strip_request_images_and_retry(agent: Any, api_messages: Any) -> bool:
    """Strip image parts from the per-call ``api_messages`` copy; True if anything was removed.

    Shared by the corrupt-image recoveries: a bad payload says nothing about the model, so it
    is stripped for this attempt only and the model is never recorded as image-rejecting."""
    from agent.turn_recovery import _strip_images_from_messages, _vlines
    if isinstance(api_messages, list) and _strip_images_from_messages(api_messages):
        _vlines(agent, "⚠️  Provider rejected a corrupted image — stripped images from the retry payload and retrying...")
        return True
    return False
