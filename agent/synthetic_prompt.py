"""Separate the human-authored part of a user-role turn from Hermes-generated prompt text.

Hermes injects many user-role turns itself: background-process and delegation notices, goal
continuations, heartbeat and ``/loop`` wakeups, cron job runs and recovery notes. They are real
execution events but poor memory signal, so automatic memory recall and transcript retention both
ask :func:`human_prompt_text` for the part a person actually wrote. One classifier keeps the two
memory paths from disagreeing about which turns are synthetic.

Provenance is read in this order:

1. A recognized generated formatter boundary. The generated prefix is dropped and only text after
   its provable end survives, because the gateway can merge a real follow-up into a queued
   notification and that follow-up must not be lost.
2. The turn's structured provenance: a runtime-owned ``display_kind`` or an unattended platform
   (``cron``: every turn of a scheduled run, not only its preamble). With no recognized boundary
   there is no trustworthy human part, so nothing survives.
3. Otherwise the whole text is human-authored.
"""

from __future__ import annotations

import re
from string import Formatter
from typing import Optional

from agent.memory_provider import is_trivial_prompt
from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN
from hermes_cli.goals import (
    CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE,
    CONTINUATION_PROMPT_TEMPLATE,
    CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
    CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE,
    CONTINUATION_REVISIONS_TEMPLATE,
    GOAL_WAIT_LIFTED_NOTE_OPEN,
    KANBAN_GOAL_CONTINUATION_TEMPLATE,
    KANBAN_GOAL_FINALIZE_TEMPLATE,
)
from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE
from hermes_cli.loops import WAKEUP_PROMPT_TEMPLATE, WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE
from tools.delegation_resume import AUTO_RESUME_NOTICE_OPEN
from tools.process_registry_notifications import (
    PROCESS_NOTICE_OPEN, PROCESS_NOTICE_OPENERS, PROCESS_NOTIFICATION_END,
)

# User-row display kinds only the runtime writes for turns it generated itself. "hidden" and
# "steer" are absent on purpose: clients submit hidden prompts and people type steers.
RUNTIME_PROMPT_DISPLAY_KINDS = frozenset({
    "internal_notification",     # gateway MessageEvent(internal=True), including heartbeats
    "process_complete",          # bot-mode process completion rows
    "async_delegation_complete",  # TUI/desktop delegation results
    "auto_continue",             # TUI crash-recovery continuation
})
# Unattended platforms: every turn is a scheduled run with no person present. A cron run's prompt
# is the generated preamble (``cron.scheduler_prompt._CRON_HINT``, ~1.1K chars, longer than
# Hindsight's default 800-char recall window), then the job's notepad, script output and stored
# task text. The whole run is treated as generated, so cron jobs neither auto-recall nor
# auto-retain by default; their explicit memory tools still work.
UNATTENDED_PLATFORMS = frozenset({"cron"})

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
_LEGACY_PROCESS_NOTICE_RE = re.compile(
    r"\[IMPORTANT: (?:Background process |\d+ background (?:processes|subagent delegations) completed)"
)


class _TemplateMatcher:
    """Anchored matcher for one formatter template, built from the template's own literals.

    Linear in the input: ``str.find`` places each interior literal at its earliest position
    (leaving the most room for the rest), then the terminal literal is chosen among its copies. A
    regex with one greedy ``.*`` per field backtracks polynomially on crafted input that repeats the
    interior literals without the terminal one, and this runs on the turn path.

    When the terminal literal occurs more than once, one copy is generated and the others are
    quoted, either inside the payload field or inside a human follow-up. The earliest copy is the
    boundary when a gateway-merged follow-up starts right after it: the pending-slot merge joins
    with exactly one newline, and a steer arrives as a blank line plus the steer marker. Otherwise
    the latest copy is the boundary, as a greedy match would choose, so payload prose that copies
    the terminal can never become a human suffix.
    """

    def __init__(self, template: str) -> None:
        literals = [literal for literal, _field, _spec, _conversion in Formatter().parse(template)]
        self.opening, self.interior, self.terminal = literals[0], literals[1:-1], literals[-1]

    def match_end(self, content: str) -> int:
        """End offset of the generated prompt at the start of ``content``, or -1."""
        if not content.startswith(self.opening):
            return -1
        position = len(self.opening)
        for literal in self.interior:
            found = content.find(literal, position)
            if found < 0:
                return -1
            position = found + len(literal)
        first = content.find(self.terminal, position)
        if first < 0:
            return -1
        first_end = first + len(self.terminal)
        if _starts_merged_follow_up(content, first_end):
            return first_end
        return content.rfind(self.terminal, position) + len(self.terminal)


def _starts_merged_follow_up(content: str, position: int) -> bool:
    """Whether a gateway-merged human follow-up starts at ``position``.

    The pending-slot text merge (``gateway.platforms.base._append_text``) joins with exactly one
    newline, and a mid-turn steer is appended as a blank line plus the steer marker.
    """
    if content.startswith("\n\n" + STEER_MARKER_OPEN, position):
        return True
    return content.startswith("\n", position) and position + 1 < len(content) and content[position + 1] != "\n"


# Match complete generated prompts from their defining templates. The formatter literals make this
# stricter than a loose prefix while the fields absorb any copied formatter prose inside untrusted
# payloads and leave only the final generated boundary; quoted terminal prose in a later human
# suffix is not itself treated as the boundary.
_INJECTED_TURN_PATTERNS = tuple((kind, template, _TemplateMatcher(template)) for kind, template in (
    ("goal", CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE),
    ("goal", CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE),
    ("goal", CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE),
    ("goal", CONTINUATION_PROMPT_TEMPLATE),
    ("kanban", KANBAN_GOAL_CONTINUATION_TEMPLATE),
    ("kanban", KANBAN_GOAL_FINALIZE_TEMPLATE),
    ("heartbeat", HEARTBEAT_PROMPT_TEMPLATE),
    ("loop", WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE),
    ("loop", WAKEUP_PROMPT_TEMPLATE),
))
_INJECTED_TURN_TERMINALS = tuple(
    matcher.terminal.strip().rsplit(". ", 1)[-1] for _kind, _template, matcher in _INJECTED_TURN_PATTERNS
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


def _unwrap_steer(suffix: str) -> str:
    if suffix.startswith(STEER_MARKER_OPEN + "\n") and suffix.endswith("\n" + STEER_MARKER_CLOSE):
        return suffix[len(STEER_MARKER_OPEN): -len(STEER_MARKER_CLOSE)].strip()
    return suffix


def _user_after_injected_turn(content: str) -> str | None:
    """Drop a generated turn, preserving only a suffix after its exact formatter boundary."""
    match_kind = None
    marker_end = -1
    for kind, _template, matcher in _INJECTED_TURN_PATTERNS:
        marker_end = matcher.match_end(content)
        if marker_end >= 0:
            match_kind = kind
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
    # The gateway's text merge joins a queued follow-up with a single newline, so one newline is a
    # real boundary after the template's terminal literal. Text glued to the terminal is not.
    # A revision block is different: its reason and earlier-requirement values may span lines, so
    # only a blank line (enforced by _revision_suffix_boundary) separates it from human text.
    if not suffix.startswith("\n"):
        return None
    suffix = suffix.strip()
    if revision_block and any(fragment and fragment in suffix for fragment in _INJECTED_TURN_TERMINALS):
        return None
    if match_kind == "goal" and suffix.startswith(GOAL_WAIT_LIFTED_NOTE_OPEN):
        # GoalManager appends one generated single-line barrier-lift note to an idle-woken continuation.
        note, _newline, rest = suffix.partition("\n")
        if not note.endswith("]"):
            return None
        suffix = rest.strip()
    return _unwrap_steer(suffix) or None


def text_after_generated_prefix(content: str, *, include_templates: bool = True) -> str | None:
    """Text after a recognized generated prefix, never its untrusted payload.

    Returns ``content`` unchanged when no generated formatter is recognized, the human suffix when
    one follows a provable boundary, and ``None`` when the generated text has no human part.
    ``include_templates=False`` checks only notice formatters (goal/heartbeat/loop templates are
    user-side prompts; assistant text keeps the historical notice rules).
    """
    if include_templates:
        injected = _user_after_injected_turn(content)
        if injected != content:
            return injected
    if not content.startswith(_MACHINE_NOTICE_PREFIXES):
        return content
    if content.startswith(PROCESS_NOTICE_OPEN) and f"\n{PROCESS_NOTIFICATION_END}" not in content:
        # Preserve historical unframed process notices, but don't classify an
        # arbitrary user-written [IMPORTANT: ...] as a process result.
        if not _LEGACY_PROCESS_NOTICE_RE.match(content):
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
    return _unwrap_steer(suffix) or None


def is_runtime_prompt(*, display_kind: Optional[str] = None, platform: Optional[str] = None) -> bool:
    """Whether the turn's structured provenance says Hermes, not a person, wrote the prompt."""
    return (display_kind in RUNTIME_PROMPT_DISPLAY_KINDS
            or str(platform or "").strip().lower() in UNATTENDED_PLATFORMS)


def human_prompt_text(
    content: Optional[str], *, display_kind: Optional[str] = None, platform: Optional[str] = None,
) -> str | None:
    """The human-authored part of a user-role turn, or ``None`` when Hermes generated all of it.

    ``display_kind`` is the turn's persisted user-row kind and ``platform`` the agent's platform.
    Both are optional so legacy rows without provenance still classify by their generated text.
    """
    text = (content or "").strip()
    generated = False
    # A recovery note can wrap another generated notice, so strip prefixes until none is left.
    while text:
        after = text_after_generated_prefix(text)
        if after == text:
            break
        text, generated = (after or "").strip(), True
    if not text:
        return None
    if not generated and is_runtime_prompt(display_kind=display_kind, platform=platform):
        return None
    return text


def auto_recall_query(
    content: Optional[str], *, display_kind: Optional[str] = None, platform: Optional[str] = None,
    include_synthetic: bool = False,
) -> str:
    """The query for automatic memory recall this turn, or ``""`` to skip it.

    Synthetic turns recall nothing by default (``memory.recall_synthetic_turns``): their generated
    text is a poor semantic query and repeats verbatim across turns. A human suffix merged into one
    still recalls, keyed on that suffix alone. Explicit memory tools are unaffected.
    """
    text = (content or "").strip()
    query = text if include_synthetic else human_prompt_text(text, display_kind=display_kind, platform=platform)
    return "" if not query or is_trivial_prompt(query) else query
