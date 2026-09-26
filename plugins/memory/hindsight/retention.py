"""Pure helpers for keeping durable chat signal in Hindsight transcripts."""

from __future__ import annotations

import re


# These prefixes are emitted by Hermes itself, not written as user intent or an
# assistant answer. Keep this list deliberately narrow: dropping real content is
# worse than retaining an occasional status line.
_MACHINE_NOTICE_PREFIXES = (
    "[ASYNC DELEGATION BATCH COMPLETE",
    "[ASYNC DELEGATION COMPLETE",
    "[ASYNC DELEGATION TASK FAILED",
    "[NATIVE REVIEW COMPLETE",
    "[SUBAGENT",
    "⚠ SUBAGENT",
    "[CONTEXT COMPACTION",
    "[CONTEXT SUMMARY",
    "[PRIOR CONTEXT",
    "[IMPORTANT: Background process",
)
_USER_FOLLOWUP_RE = re.compile(
    r"\b(?:please|can you|could you|would you|i want|i need|investigate|remember|keep|fix|add|"
    r"remove|change|explain|summari[sz]e|review|check|look at|tell me|describe|compare|help|"
    r"show|run|write|make|continue|proceed|what do you think|should we)\b",
    re.IGNORECASE,
)
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


def _is_machine_notice(content: str, *, user_message: bool = False) -> bool:
    if not content.startswith(_MACHINE_NOTICE_PREFIXES):
        return False
    if not user_message:
        return True
    # Hermes may place a generated notification in the user slot. Drop a
    # standalone notice, but keep a real request appended by the user.
    suffix = content.splitlines()[1:]
    return not _USER_FOLLOWUP_RE.search("\n".join(suffix))


def _is_assistant_status_only(content: str) -> bool:
    if content in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}:
        return True
    return "\n" not in content and bool(_ASSISTANT_STATUS_ONLY_RE.fullmatch(content))


def filter_retain_messages(user_content: str, assistant_content: str) -> tuple[str | None, str | None]:
    """Return the durable parts of one user/assistant turn.

    Machine notices and recalled memory context are excluded. User messages are
    never classified by generic status wording; only known Hermes-injected
    prefixes are dropped. Assistant text loses only exact silence markers and a
    small, explicit set of one-line status-only responses.
    """
    user = _clean_message(user_content, preserve_unmatched_literal=True)
    assistant = _clean_message(assistant_content)
    if not user or _is_machine_notice(user, user_message=True):
        user = None
    if not assistant or _is_machine_notice(assistant) or _is_assistant_status_only(assistant):
        assistant = None
    return user, assistant
