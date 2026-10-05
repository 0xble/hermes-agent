"""Pure helpers for keeping durable chat signal in Hindsight transcripts."""

from __future__ import annotations

import re
from string import Formatter

from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN
from hermes_cli.goals import (
    CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE,
    CONTINUATION_PROMPT_TEMPLATE,
    CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
    CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE,
    CONTINUATION_REVISIONS_TEMPLATE,
    KANBAN_GOAL_CONTINUATION_TEMPLATE,
)
from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE
from hermes_cli.loops import WAKEUP_PROMPT_TEMPLATE, WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE
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

def _template_pattern(template: str) -> re.Pattern[str]:
    """Build an anchored matcher from a formatter template without copying its literals."""
    parts = ["^"]
    for literal, field_name, _format_spec, _conversion in Formatter().parse(template):
        parts.append(re.escape(literal))
        if field_name is not None:
            parts.append(".*?")
    return re.compile("".join(parts), re.DOTALL)


def _template_terminal(template: str) -> str:
    literals = [literal for literal, _field, _spec, _conversion in Formatter().parse(template)]
    return literals[-1]


# Match complete generated prompts from their defining templates. The formatter literals make this
# stricter than a loose prefix while the non-greedy fields stop at the actual generated boundary;
# quoted terminal prose in a goal or later human suffix is not itself treated as the boundary.
_INJECTED_TURN_PATTERNS = (
    ("goal", CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE, _template_pattern(CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE)),
    ("goal", CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE, _template_pattern(CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE)),
    ("goal", CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE, _template_pattern(CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE)),
    ("goal", CONTINUATION_PROMPT_TEMPLATE, _template_pattern(CONTINUATION_PROMPT_TEMPLATE)),
    ("kanban", KANBAN_GOAL_CONTINUATION_TEMPLATE, _template_pattern(KANBAN_GOAL_CONTINUATION_TEMPLATE)),
    ("heartbeat", HEARTBEAT_PROMPT_TEMPLATE, _template_pattern(HEARTBEAT_PROMPT_TEMPLATE)),
    ("loop", WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE, _template_pattern(WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE)),
    ("loop", WAKEUP_PROMPT_TEMPLATE, _template_pattern(WAKEUP_PROMPT_TEMPLATE)),
)
_INJECTED_TURN_TERMINALS = tuple(
    terminal.strip().rsplit(". ", 1)[-1] for terminal in
    (_template_terminal(template) for _kind, template, _pattern in _INJECTED_TURN_PATTERNS)
)
_REVISION_BLOCK_PREFIX = CONTINUATION_REVISIONS_TEMPLATE.split("{revision_lines}", 1)[0]
_REVISION_ENTRY_RE = re.compile(r"- v\d+ \(")


def _revision_suffix_boundary(content: str, start: int) -> int | None:
    """Return a boundary only for a structurally complete rendered revision block."""
    position = start
    saw_entry = False
    while position < len(content):
        line_end = content.find("\n", position)
        if line_end < 0:
            line_end = len(content)
        line = content[position:line_end]
        if _REVISION_ENTRY_RE.match(line):
            saw_entry = True
        elif line.startswith(("    earlier ", "    dropped criteria: ")) and saw_entry:
            pass
        else:
            break
        position = line_end + (line_end < len(content))
    if not saw_entry:
        return None
    first_text = position
    while first_text < len(content) and content[first_text] == "\n":
        first_text += 1
    if first_text == len(content) or first_text - position < 1:
        return None
    return position - 1


def _user_after_injected_turn(content: str) -> str | None:
    """Drop a generated turn, preserving only a suffix after its exact formatter boundary."""
    match_kind = None
    marker_end = -1
    for kind, _template, pattern in _INJECTED_TURN_PATTERNS:
        match = pattern.match(content)
        if match:
            match_kind = kind
            marker_end = match.end()
            break
    if marker_end < 0:
        return content

    revision_block = False
    if match_kind == "goal" and content.startswith(_REVISION_BLOCK_PREFIX, marker_end):
        revision_block = True
        revision_start = marker_end + len(_REVISION_BLOCK_PREFIX)
        revision_boundary = _revision_suffix_boundary(content, revision_start)
        marker_end = revision_boundary if revision_boundary is not None else len(content)

    suffix = content[marker_end:]
    if not suffix.strip():
        return None
    if not suffix.startswith("\n\n"):
        # Never retain text from inside an injected payload when its boundary is ambiguous.
        return None
    suffix = suffix.strip()
    if revision_block and any(fragment and fragment in suffix for fragment in _INJECTED_TURN_TERMINALS):
        return None
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
