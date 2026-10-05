"""Pure helpers for keeping durable chat signal in Hindsight transcripts."""

from __future__ import annotations

import re

from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN
from hermes_cli.goals import (
    GOAL_CONTINUATION_PREFIX,
    GOAL_GATE_FAILED_PREFIX,
    KANBAN_GOAL_CONTINUATION_PREFIX,
)
from hermes_cli.heartbeat import HEARTBEAT_PROMPT_PREFIX
from hermes_cli.loops import LOOP_COMPLETE_MARKER, WAKEUP_PROMPT_PREFIX
from tools.delegation_resume import AUTO_RESUME_NOTICE_OPEN
from tools.process_registry_notifications import (
    PROCESS_NOTICE_OPEN, PROCESS_NOTIFICATION_END, PROCESS_NOTICE_OPENERS,
)

# Legacy unframed rows have no reliable payload boundary, so only these exact
# historical forms are dropped wholesale. Framed rows use the defining
# formatter's openers and end marker instead of a copied list of variants.
_LEGACY_MACHINE_NOTICE_PREFIXES = (
    "[NATIVE REVIEW COMPLETE",
    "[SUBAGENT",
    "⚠ SUBAGENT",
    "[CONTEXT COMPACTION",
    "[CONTEXT SUMMARY",
    "[PRIOR CONTEXT",
    "[System note:",
)
_MACHINE_NOTICE_PREFIXES = (*PROCESS_NOTICE_OPENERS, AUTO_RESUME_NOTICE_OPEN, *_LEGACY_MACHINE_NOTICE_PREFIXES)

# Synthetic turn templates are user-visible prompts but not user-authored durable signal. Keep the
# exact generated opening and terminal sentence here so a human suffix can survive without ever
# retaining the injected goal/task/heartbeat payload itself.
_GOAL_REVISIONS_END_MARKER = "When in doubt, honor the earlier requirement."
_INJECTED_TURN_END_MARKERS = (
    "(exact identifiers or output lines from tool results, in backticks), and stop. If you are blocked and need input from the user, say so clearly and stop.",
    "Before claiming the goal is done, audit each Verification item against current state and end with an Evidence section that quotes exact identifiers or output lines from tool results in backticks (commit SHAs, run ids, URLs, `N passed` lines), so the runtime can locate them. If you hit the stated stop condition or are otherwise blocked and need user input, say so clearly and stop.",
    "If you believe the goal and every additional criterion are complete, state so explicitly and stop. If you are blocked and need input from the user, say so clearly and stop.",
    "Fix the underlying problem so this gate passes, then re-run it to confirm. Do not declare the goal complete while any gate fails. If the gate itself is wrong or cannot pass, say so clearly and stop.",
    "Take the next concrete step toward completing the task. When the work is genuinely finished, call kanban_complete with a summary. If it is a code change that needs same-card review before counting as done, call kanban_request_review with a summary instead. If you are blocked and need human input, call kanban_block with a reason. Do not stop without calling one of them.",
    "If there is nothing meaningful to do or report for this instruction right now, reply briefly that nothing has changed and stop — do not invent work.",
    f"If the task is now complete, no longer applicable, or the thing you were watching has finished, say so and end your reply with {LOOP_COMPLETE_MARKER} on its own line — that stops the loop.",
    f"If the stop condition is met, or the task is no longer applicable, say so and end your reply with {LOOP_COMPLETE_MARKER} on its own line — that stops the loop.",
)
_INJECTED_TURN_PREFIXES = (
    GOAL_CONTINUATION_PREFIX,
    GOAL_GATE_FAILED_PREFIX,
    KANBAN_GOAL_CONTINUATION_PREFIX,
)


def _user_after_injected_turn(content: str) -> str | None:
    """Drop a generated turn, preserving only a suffix after its exact formatter boundary."""
    if not content.startswith(_INJECTED_TURN_PREFIXES):
        heartbeat = content.startswith(HEARTBEAT_PROMPT_PREFIX)
        loop = content.startswith(WAKEUP_PROMPT_PREFIX)
        if not (heartbeat or loop):
            return content
        # Heartbeat interval and loop tick/cadence are generated fields. Require their generated
        # line shape so a human's merely similar bracketed text is not classified as machine input.
        header, separator, remainder = content.partition("]\n")
        if heartbeat:
            interval = header[len(HEARTBEAT_PROMPT_PREFIX):]
            if not separator or not re.fullmatch(r"\d+[smhd]", interval):
                return content
        else:
            tick_and_cadence = header[len(WAKEUP_PROMPT_PREFIX):]
            tick, separator_cadence, cadence = tick_and_cadence.partition(", ")
            valid_cadence = (
                cadence == "self-paced"
                or bool(re.fullmatch(r"self-paced, currently \d+[smhd]", cadence))
                or bool(re.fullmatch(r"every \d+[smhd]", cadence))
            )
            if not separator or not remainder.startswith("Recurring task:") or not tick.isdigit() or (
                separator_cadence and not valid_cadence
            ) or (not separator_cadence and cadence):
                return content

    marker_end = -1
    if content.startswith((GOAL_CONTINUATION_PREFIX, GOAL_GATE_FAILED_PREFIX)):
        revisions_start = content.find("\n\nThis goal has been revised.")
        if revisions_start >= 0:
            revision_end = content.find(_GOAL_REVISIONS_END_MARKER, revisions_start)
            if revision_end >= 0:
                revisions_end = revision_end + len(_GOAL_REVISIONS_END_MARKER)
                suffix_boundary = content.find("\n\n", revisions_end)
                marker_end = suffix_boundary if suffix_boundary >= 0 else len(content)
    if marker_end < 0:
        marker_positions = [
            (position, marker) for marker in _INJECTED_TURN_END_MARKERS
            if (position := content.find(marker)) >= 0
        ]
        if marker_positions:
            marker_start, marker = min(marker_positions)
            marker_end = marker_start + len(marker)
    if marker_end < 0:
        # A truncated or unrecognized synthetic block has no trustworthy boundary.
        return None
    suffix = content[marker_end:]
    if not suffix.strip():
        return None
    if not suffix.startswith("\n\n"):
        # Never retain text from inside an injected payload when its boundary is ambiguous.
        return None
    suffix = suffix.strip()
    if suffix.startswith(STEER_MARKER_OPEN + "\n") and suffix.endswith("\n" + STEER_MARKER_CLOSE):
        suffix = suffix[len(STEER_MARKER_OPEN): -len(STEER_MARKER_CLOSE)].strip()
    return suffix or None


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


def _user_after_machine_notice(content: str, *, include_injected: bool = True) -> str | None:
    """Keep only text after a machine formatter boundary, never its untrusted payload."""
    if include_injected:
        injected = _user_after_injected_turn(content)
        if injected != content:
            return injected
    if not content.startswith(_MACHINE_NOTICE_PREFIXES):
        return content
    if content.startswith(PROCESS_NOTICE_OPEN) and f"\n{PROCESS_NOTIFICATION_END}" not in content:
        # Preserve historical unframed process notices, but don't classify an
        # arbitrary user-written [IMPORTANT: ...] as a process result.
        if not re.match(r"\[IMPORTANT: (?:Background process |\d+ background (?:processes|subagent delegations) completed)", content):
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
    # Upstream appends machine provenance after the process payload boundary.
    # Strip only the known complete footer forms, preserving any later human suffix.
    from gateway.run_notifications import _INTERNAL_NOTIFICATION_FOOTERS
    for footer in _INTERNAL_NOTIFICATION_FOOTERS:
        if suffix.startswith(footer):
            suffix = suffix[len(footer):].strip()
            break
    if suffix.startswith(STEER_MARKER_OPEN + "\n") and suffix.endswith("\n" + STEER_MARKER_CLOSE):
        suffix = suffix[len(STEER_MARKER_OPEN): -len(STEER_MARKER_CLOSE)].strip()
    return suffix or None


def _is_machine_notice(content: str) -> bool:
    # Synthetic-turn filtering is user-side only; assistant text keeps the historical notice rules.
    return _user_after_machine_notice(content, include_injected=False) is None


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
