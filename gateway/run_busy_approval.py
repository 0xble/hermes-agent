"""Typed approval-reply vocabulary, extracted from gateway/run_busy.py."""
from __future__ import annotations

from agent.i18n import DEFAULT_LANGUAGE, t


def approval_input_words(input_key: str) -> tuple[str, ...]:
    """Typed-reply synonyms for ``approval.inputs.<input_key>`` (comma-lists): the English words
    ALWAYS match, plus the active language's list, so a Polish pack can add "tak" without losing
    "yes". Lower-cased, de-duplicated, order preserved (English first)."""
    seen: dict[str, None] = {}
    key = f"approval.inputs.{input_key}"
    for raw in (t(key, lang=DEFAULT_LANGUAGE), t(key)):
        if raw == key:  # catalog miss: t() echoes the key
            continue
        for word in raw.split(","):
            word = word.strip().lower()
            if word:
                seen.setdefault(word, None)
    return tuple(seen)
