"""Exact-value redaction of secrets that transited a server-side vault fill (browser_vault).

Re-exported by ``agent.redact``. The registry also owns the fork's tab/origin-scoped
date and card components, so browser egress and generic redaction share one state seam.
"""

from __future__ import annotations

import re
import threading
from typing import Any

# ---------------------------------------------------------------------------
# Vault-value redaction registry (profile-scoped, bounded)
# ---------------------------------------------------------------------------
# Exact secret values that transited a server-side vault fill (browser_vault). Generic
# credential-shaped regexes cannot catch an arbitrary user password, so the fill path registers
# the exact bytes and every browser_* tool result (including browser_cdp Runtime.evaluate
# passthrough) is scrubbed against them before it can reach the model. Memory only: never
# persisted or logged. Keyed by profile home so a multiplex gateway never scrubs profile B's
# output with profile A's passwords (which would also confirm to B that the bytes exist), and
# bounded per profile: a fill-heavy session evicts its oldest entries rather than growing forever.
_VAULT_REDACTION_MAX_PER_PROFILE = 64
_VAULT_REDACTION_VALUES: dict = {}  # profile home → ordered {value: None}
_VAULT_DATE_PARTS: dict = {}  # (profile home, tab key, origin) → ordered {(component, value)}
_VAULT_CARD_PARTS: dict = {}  # same scope → separately bounded card metadata components
_VAULT_REDACTION_LOCK = threading.Lock()
_REDACTION_GENERATION = 0


def redaction_generation() -> int:
    """Return a process-wide generation for dynamic egress redaction rules."""
    return _REDACTION_GENERATION


def _vault_scope() -> str:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


def register_vault_redaction_value(value) -> None:
    """Register an exact vault secret value for model-facing redaction.

    Called by the vault fill path BEFORE the injection happens, so no later browser tool result
    can echo the value back into model context. Also registers the form a text input normalizes
    it to (CR/LF stripped), since that is what the page holds.
    """
    if not isinstance(value, str) or not value:
        return
    normalized = value.replace("\r", "").replace("\n", "")
    global _REDACTION_GENERATION
    with _VAULT_REDACTION_LOCK:
        bucket = _VAULT_REDACTION_VALUES.setdefault(_vault_scope(), {})
        for v in (value, normalized):
            if v:
                bucket.pop(v, None)  # re-registering refreshes recency
                bucket[v] = None
        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:
            del bucket[next(iter(bucket))]
        _REDACTION_GENERATION += 1


def _date_scope(tab: str, origin: str) -> tuple[str, str, str]:
    return (_vault_scope(), str(tab), str(origin).lower())


def register_vault_date_component(token: str, value: str, *,
                                  tab: str = "default", origin: str = "") -> None:
    """Register one exact date component for a browser tab and its filled origin."""
    if token not in ("bday-month", "bday-day", "bday-year") or not isinstance(value, str) or not value.isdigit():
        return
    global _REDACTION_GENERATION
    with _VAULT_REDACTION_LOCK:
        bucket = _VAULT_DATE_PARTS.setdefault(_date_scope(tab, origin), {})
        key = (token.removeprefix("bday-"), value)
        bucket.pop(key, None)
        bucket[key] = None
        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:
            del bucket[next(iter(bucket))]
        _REDACTION_GENERATION += 1


def register_vault_card_component(field: str, value: str, *, tab: str, origin: str) -> None:
    """Keep low-entropy card metadata on its filled tab and origin, not in the global secret set.

    An unavailable origin is conservatively tab-scoped until the existing browser cleanup.
    """
    if field not in {"cardholder_name", "exp_month", "exp_year", "billing_postal_code", "cvc", "expiry"} or not isinstance(value, str) or not value:
        return
    global _REDACTION_GENERATION
    with _VAULT_REDACTION_LOCK:
        bucket = _VAULT_CARD_PARTS.setdefault(_date_scope(tab, origin), {})
        key = (field, value)
        bucket.pop(key, None)
        bucket[key] = None
        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:
            del bucket[next(iter(bucket))]
        _REDACTION_GENERATION += 1


def mark_vault_protected_tab(tab: str, origin: str) -> None:
    """Record a protected fill on a tab/origin even when no short component exists
    (a combined ``input[type=date]``): pixel capture must still be refused there."""
    global _REDACTION_GENERATION
    with _VAULT_REDACTION_LOCK:
        _VAULT_DATE_PARTS.setdefault(_date_scope(tab, origin), {})
        _REDACTION_GENERATION += 1


def vault_read_has_protected_field_context(expression: str) -> bool:
    """Legacy API retained for third-party callers; no redaction path consults it."""
    return False


def clear_vault_date_components(tab: str, origin: str | None = None) -> None:
    """Forget a tab's birthday and card components on close or cross-origin navigation."""
    global _REDACTION_GENERATION
    with _VAULT_REDACTION_LOCK:
        changed = False
        for registry in (_VAULT_DATE_PARTS, _VAULT_CARD_PARTS):
            for key in list(registry):
                if key[:2] == (_vault_scope(), str(tab)) and (origin is None or key[2] != str(origin).lower()):
                    del registry[key]
                    changed = True
        if changed:
            _REDACTION_GENERATION += 1


def has_vault_date_components(tab: str) -> bool:
    with _VAULT_REDACTION_LOCK:
        return any(key[:2] == (_vault_scope(), str(tab)) for key in _VAULT_DATE_PARTS)


def has_vault_scoped_components(tab: str) -> bool:
    """Whether browser egress should resolve this session's current origin."""
    with _VAULT_REDACTION_LOCK:
        return any(key[:2] == (_vault_scope(), str(tab))
                   for registry in (_VAULT_DATE_PARTS, _VAULT_CARD_PARTS) for key in registry)


def has_any_vault_date_components() -> bool:
    """A stateless CDP endpoint has no task ownership; deny it while any page is protected."""
    with _VAULT_REDACTION_LOCK:
        return any(key[0] == _vault_scope() for key in _VAULT_DATE_PARTS)


def _date_parts(tab: str | None, origin: str = "") -> tuple:
    """Keep birthday semantics; card parts need an owned tab, not necessarily a known origin."""
    with _VAULT_REDACTION_LOCK:
        merged = {}
        scope = (_vault_scope(), "default" if tab is None else str(tab))
        for key, bucket in _VAULT_DATE_PARTS.items():
            if key[:2] == scope:
                merged.update(bucket)
        # Omitted browser context must not turn the task named "default" into
        # a source of short card masks for generic terminal/log output.
        for key, bucket in _VAULT_CARD_PARTS.items():
            if tab is not None and key[:2] == scope and (not key[2] or not origin or key[2] == origin.lower()):
                merged.update(bucket)
        return tuple(merged)


def _expiry_pattern(value: str) -> str | None:
    """Derive equivalents without spending bounded registry slots or inventing a century."""
    if not value.isascii():
        return None
    match = (re.fullmatch(r"(0?[1-9]|1[0-2])\s*/\s*([0-9]{4}|[0-9]{2})", value)
             or re.fullmatch(r"(0[1-9]|1[0-2])([0-9]{4}|[0-9]{2})", value))
    if match is None:
        return None
    month, year = match.groups()
    month = str(int(month))
    month_pattern = f"0?{month}" if len(month) == 1 else month
    year_pattern = f"(?:{year}|{year[-2:]})" if len(year) == 4 else year
    return rf"{month_pattern}(?:\s*/\s*)?{year_pattern}"


def redact_registered_vault_number(value: Any, *,
                                   tab: str | None = None, origin: str = "") -> Any:
    """Scrub registered global secrets and scoped components returned as numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    text = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    with _VAULT_REDACTION_LOCK:
        registered = text in _VAULT_REDACTION_VALUES.get(_vault_scope(), ())
    parts = _date_parts(tab, origin)
    if registered or any(text == part for _, part in parts):
        return "«redacted-vault-secret»"
    if any(field == "expiry" and (pattern := _expiry_pattern(part)) is not None
           and re.fullmatch(pattern, text) for field, part in parts):
        return "«redacted-vault-secret»"
    return value


def clear_vault_redaction_values() -> None:
    """Drop the current profile's registered values (profile teardown / explicit lock)."""
    global _REDACTION_GENERATION
    with _VAULT_REDACTION_LOCK:
        changed = _vault_scope() in _VAULT_REDACTION_VALUES
        _VAULT_REDACTION_VALUES.pop(_vault_scope(), None)
        for registry in (_VAULT_DATE_PARTS, _VAULT_CARD_PARTS):
            for key in list(registry):
                if key[0] == _vault_scope():
                    del registry[key]
                    changed = True
        if changed:
            _REDACTION_GENERATION += 1


def redact_registered_vault_values(text: str, *,
                                   tab: str | None = None, origin: str = "") -> str:
    """Redact exact vault values and date components on the owning tab and origin."""
    if not isinstance(text, str) or not text:
        return text
    with _VAULT_REDACTION_LOCK:
        bucket = _VAULT_REDACTION_VALUES.get(_vault_scope())
        values = sorted(bucket, key=len, reverse=True) if bucket else ()
    parts = _date_parts(tab, origin)
    for value in values:
        if value.isascii() and value.isdigit() and 13 <= len(value) <= 19:
            # Checkout masks commonly insert spaces, NBSPs or dashes into a
            # registered PAN. Match only this known number, not arbitrary PANs.
            pattern = r"(?<!\d)" + r"[\s-]*".join(value) + r"(?!\d)"
            text = re.sub(pattern, "«redacted-vault-secret»", text)
        if value in text:
            text = text.replace(value, "«redacted-vault-secret»")
    # Full expiries must be consumed before month/year components can split them.
    for field, value in parts:
        if field == "expiry" and (pattern := _expiry_pattern(value)) is not None:
            text = re.sub(rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])",
                          "«redacted-vault-secret»", text)
    for _, value in sorted(parts, key=lambda part: len(part[1]), reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])",
                      "«redacted-vault-secret»", text)
    return text


def redact_registered_vault_snapshot(text: str, *, tab: str | None = None, origin: str = "") -> str:
    """Apply the same tab/origin component rule used by every browser output."""
    return redact_registered_vault_values(text, tab=tab, origin=origin)
