"""Pure helpers for keeping durable chat signal in Hindsight transcripts."""

from __future__ import annotations

import re

from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN
from tools.process_registry_notifications import PROCESS_NOTIFICATION_END

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
    "[System note:",
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


def _user_after_machine_notice(content: str) -> str | None:
    """Keep only text after the formatter's boundary, never its untrusted payload."""
    if not content.startswith(_MACHINE_NOTICE_PREFIXES):
        return content
    # Gateway recovery notes are a closed bracket followed by a blank line
    # before any real user message (gateway.run.build_resume_recovery_note).
    if content.startswith("[System note:"):
        _, boundary, suffix = content.partition("]\n\n")
        return suffix.strip() or None if boundary else None
    # A legacy, unframed notice has no provable end: it cannot yield a
    # trustworthy suffix, even if the goal/result contains request-like words.
    _, boundary, suffix = content.rpartition(f"\n{PROCESS_NOTIFICATION_END}")
    if not boundary:
        return None
    suffix = suffix.strip()
    if suffix.startswith(STEER_MARKER_OPEN + "\n") and suffix.endswith("\n" + STEER_MARKER_CLOSE):
        suffix = suffix[len(STEER_MARKER_OPEN): -len(STEER_MARKER_CLOSE)].strip()
    return suffix or None


def _is_machine_notice(content: str) -> bool:
    return content.startswith(_MACHINE_NOTICE_PREFIXES)


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
    user = _user_after_machine_notice(user_content.strip())
    user = _clean_message(user, preserve_unmatched_literal=True) or None if user else None
    assistant = _clean_message(assistant_content)
    if not assistant or _is_machine_notice(assistant) or _is_assistant_status_only(assistant):
        assistant = None
    return user, assistant
