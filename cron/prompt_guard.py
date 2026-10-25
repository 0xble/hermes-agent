"""Refuse cron prompt rewrites that look like an unexpanded placeholder or an accidental wipe.

A prompt update replaces the job's whole instruction. An agent that meant to load the
prompt from a file has twice persisted the literal token instead (``__FILE__`` over an
11k-char prompt). Both update surfaces (the ``cronjob_manage`` tool and ``hermes cron
edit``) call :func:`prompt_update_error`; ``allow_shrink`` is the explicit override.
Creation is not guarded: there is no existing prompt to lose.
"""

from __future__ import annotations

import re
from typing import Optional

SHRINK_MIN_EXISTING_CHARS = 500
SHRINK_MIN_RATIO = 0.10

_PLACEHOLDER_PATTERNS = tuple(re.compile(p, re.IGNORECASE | re.DOTALL) for p in (
    r"__[A-Z0-9_]+__",                 # __FILE__, __PROMPT__
    r"<[^<>\n]{1,60}>",                # <prompt>, <full prompt here>
    r"(?:\.{3,}|\u2026+)",             # ... or …
    r"(?:TODO|TBD|FIXME|PLACEHOLDER)",
    r"\$\(\s*(?:cat|<)\s[^)]*\)",      # $(cat prompt.md), $(< prompt.md)
    r"`\s*cat\s[^`]*`",                # `cat prompt.md`
    r"@[\w./~-]+",                     # @prompt.md
    r"\$\{?[A-Z_][A-Z0-9_]*\}?",       # $PROMPT, ${PROMPT}
))


def _is_placeholder(text: str) -> bool:
    return any(p.fullmatch(text) for p in _PLACEHOLDER_PATTERNS)


def prompt_update_error(old_prompt: Optional[str], new_prompt: str, *, allow_shrink: bool = False) -> Optional[str]:
    """Refusal message for replacing ``old_prompt`` with ``new_prompt``, or None when allowed."""
    if allow_shrink:
        return None
    old_len = len((old_prompt or "").strip())
    new_text = (new_prompt or "").strip()
    new_len = len(new_text)
    lengths = f"existing prompt {old_len} chars, new prompt {new_len} chars"
    if new_text and _is_placeholder(new_text):
        return (f"Refused prompt update: the new prompt looks like an unexpanded placeholder "
                f"({new_text[:40]!r}; {lengths}). Pass the full prompt text. If this literal "
                f"prompt is intended, retry with allow_shrink=true (CLI: --allow-shrink).")
    if old_len >= SHRINK_MIN_EXISTING_CHARS and new_len < old_len * SHRINK_MIN_RATIO:
        return (f"Refused prompt update: the new prompt would shrink the job's prompt by more "
                f"than {100 - int(SHRINK_MIN_RATIO * 100)}% ({lengths}). Prompt updates replace "
                f"the whole prompt. If the shorter prompt is intended, retry with "
                f"allow_shrink=true (CLI: --allow-shrink).")
    return None
