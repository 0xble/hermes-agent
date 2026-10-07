"""Pure helpers for keeping durable chat signal in Hindsight transcripts."""

from __future__ import annotations

import re

from agent.synthetic_prompt import human_prompt_text, text_after_generated_prefix

_MEMORY_CONTEXT_BLOCK_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>",
    re.IGNORECASE,
)
_MEMORY_CONTEXT_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_ASSISTANT_STATUS_ONLY_RE = re.compile(
    r"(?:done|completed|complete|ok|okay|acknowledged|noted|in progress|working on it|"
    r"no changes(?: detected)?|no action needed|status\s*:\s*(?:done|completed|complete|ok|"
    r"success(?:ful)?|in progress|pending|failed|error|timeout))[.!]?",
    re.IGNORECASE,
)


def _clean_message(content: str, *, preserve_unmatched_literal: bool = False) -> str:
    """Remove recalled context while preserving any surrounding real text."""
    cleaned = _MEMORY_CONTEXT_BLOCK_RE.sub("", content or "")
    # A truncated/malformed injected block has no closing tag. Drop a block
    # that starts the message; otherwise remove only the tag and preserve the
    # surrounding text because the tag may be literal user content.
    open_match = re.search(r"<\s*memory-context\s*>", cleaned, re.IGNORECASE)
    if open_match and preserve_unmatched_literal and not cleaned[:open_match.start()].strip():
        cleaned = ""
    elif open_match and not preserve_unmatched_literal:
        if not cleaned[:open_match.start()].strip():
            cleaned = ""
        else:
            cleaned = cleaned[:open_match.start()] + cleaned[open_match.end():]
    if _MEMORY_CONTEXT_TAG_RE.search(cleaned) and not (open_match and preserve_unmatched_literal and cleaned):
        cleaned = _MEMORY_CONTEXT_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def _is_machine_notice(content: str) -> bool:
    # Synthetic-turn filtering is user-side only; assistant text keeps the historical notice rules.
    return text_after_generated_prefix(content, include_templates=False) is None


def _is_assistant_status_only(content: str) -> bool:
    if content in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}:
        return True
    return "\n" not in content and bool(_ASSISTANT_STATUS_ONLY_RE.fullmatch(content))


def filter_retain_messages(
    user_content: str, assistant_content: str, *, display_kind: str | None = None,
    platform: str | None = None,
) -> tuple[str | None, str | None]:
    """Return the durable parts of one user/assistant turn.

    The user side keeps only its human-authored part (``agent.synthetic_prompt``, shared with the
    auto-recall gate): generated notices and prompts are dropped by their formatter boundary or by
    the turn's runtime-owned provenance, never by generic status wording. Recalled memory context is
    excluded. Assistant text loses only exact silence markers and a small, explicit set of one-line
    status-only responses.
    """
    user = human_prompt_text(user_content, display_kind=display_kind, platform=platform)
    user = _clean_message(user, preserve_unmatched_literal=True) or None if user else None
    assistant = _clean_message(assistant_content)
    if not assistant or _is_machine_notice(assistant) or _is_assistant_status_only(assistant):
        assistant = None
    return user, assistant
