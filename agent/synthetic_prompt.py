"""Separate the human-authored part of a user-role turn from Hermes-generated prompt text.

Hermes injects many user-role turns itself: background-process and delegation notices, goal
continuations, heartbeat and ``/loop`` wakeups, cron job runs and recovery notes. They are real
execution events but poor memory signal, so automatic memory recall and transcript retention both
ask :func:`human_prompt_text` for the part a person actually wrote. One classifier keeps the two
memory paths from disagreeing about which turns are synthetic.

Provenance is read in this order:

1. The turn's structured provenance: a runtime-owned ``display_kind`` or an unattended platform
   (``cron``: every turn of a scheduled run, not only its preamble). It is authoritative, so the
   whole turn is generated and text matching never overrides it. A person's message the gateway
   text-merged into such a turn is still answered but is not used for memory.
2. A recognized generated formatter boundary. The generated prefix is dropped and only text after
   its provable end survives, because the gateway can merge a real follow-up into a queued goal,
   kanban or ``/loop`` prompt that carries no runtime ``display_kind``, and legacy rows have none.
3. Otherwise the whole text is human-authored.
"""

from __future__ import annotations

import re
from string import Formatter
from typing import Optional

from agent.memory_provider import is_trivial_prompt
from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN
from hermes_cli.goals import KANBAN_GOAL_CONTINUATION_TEMPLATE, KANBAN_GOAL_FINALIZE_TEMPLATE
from hermes_cli.goals_prompt_registry import GOAL_PROMPT_REGISTRY
from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE, _PREVIOUS_HEARTBEAT_PROMPT_TEMPLATE
from hermes_cli.loops import (
    WAKEUP_PROMPT_TEMPLATE,
    WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
    _GOAL_RULES_WAKEUP_PROMPT_TEMPLATE,
    _GOAL_RULES_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
    _ORIGINAL_WAKEUP_PROMPT_TEMPLATE,
    _ORIGINAL_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
    _PREVIOUS_WAKEUP_PROMPT_TEMPLATE,
    _PREVIOUS_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
    _SILENCE_CONTRACT_WAKEUP_PROMPT_TEMPLATE,
    _SILENCE_CONTRACT_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
)
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
    (leaving the most room for the rest), then ``str.rfind`` takes the LAST copy of the terminal
    literal. A regex with one greedy ``.*`` per field backtracks polynomially on crafted input that
    repeats the interior literals without the terminal one, and this runs on the turn path.

    The last copy is the boundary by rule, not by guessing. A copy inside the payload field always
    precedes the generated one, so payload text can never become a human suffix. A copy that a
    person quoted in a merged follow-up comes after it, so only the human text after that quote
    survives. Text alone cannot tell the two apart, and the safe side is never to call generated
    text human.
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
        last = content.rfind(self.terminal, position)
        return -1 if last < 0 else last + len(self.terminal)


# Match complete generated prompts from every defining goal formatter wording ever shipped. The
# registry is generated from origin/main and this branch, so persisted rows from old releases remain
# synthetic even after a formatter wording changes.
_GOAL_PROMPT_PATTERNS = tuple(
    ("goal", str(row["template"]), _TemplateMatcher(str(row["template"])))
    for row in GOAL_PROMPT_REGISTRY
    if row["formatter"] in {"autonomy", "plain", "contract", "subgoals", "gate_failed"}
)
_INJECTED_TURN_PATTERNS = (
    *_GOAL_PROMPT_PATTERNS,
    ("kanban", KANBAN_GOAL_CONTINUATION_TEMPLATE, _TemplateMatcher(KANBAN_GOAL_CONTINUATION_TEMPLATE)),
    ("kanban", KANBAN_GOAL_FINALIZE_TEMPLATE, _TemplateMatcher(KANBAN_GOAL_FINALIZE_TEMPLATE)),
    ("heartbeat", HEARTBEAT_PROMPT_TEMPLATE, _TemplateMatcher(HEARTBEAT_PROMPT_TEMPLATE)),
    ("loop", WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE, _TemplateMatcher(WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE)),
    ("loop", WAKEUP_PROMPT_TEMPLATE, _TemplateMatcher(WAKEUP_PROMPT_TEMPLATE)),
    ("heartbeat", _PREVIOUS_HEARTBEAT_PROMPT_TEMPLATE, _TemplateMatcher(_PREVIOUS_HEARTBEAT_PROMPT_TEMPLATE)),
    ("loop", _PREVIOUS_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE, _TemplateMatcher(_PREVIOUS_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE)),
    ("loop", _PREVIOUS_WAKEUP_PROMPT_TEMPLATE, _TemplateMatcher(_PREVIOUS_WAKEUP_PROMPT_TEMPLATE)),
    ("loop", _SILENCE_CONTRACT_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE, _TemplateMatcher(_SILENCE_CONTRACT_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE)),
    ("loop", _SILENCE_CONTRACT_WAKEUP_PROMPT_TEMPLATE, _TemplateMatcher(_SILENCE_CONTRACT_WAKEUP_PROMPT_TEMPLATE)),
    ("loop", _GOAL_RULES_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE, _TemplateMatcher(_GOAL_RULES_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE)),
    ("loop", _GOAL_RULES_WAKEUP_PROMPT_TEMPLATE, _TemplateMatcher(_GOAL_RULES_WAKEUP_PROMPT_TEMPLATE)),
    ("loop", _ORIGINAL_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE, _TemplateMatcher(_ORIGINAL_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE)),
    ("loop", _ORIGINAL_WAKEUP_PROMPT_TEMPLATE, _TemplateMatcher(_ORIGINAL_WAKEUP_PROMPT_TEMPLATE)),
)
# Opens the revision history older GoalManagers appended after a revised goal's continuation. Its lines
# carry earlier goal text, revision reasons and quoted user messages, any of which may span lines
# or paragraphs, and it has no closing marker, so no text after it can be proven human.
_REVISION_BLOCK_OPENS = tuple(
    str(row["template"]).split("{revision_lines}", 1)[0]
    for row in GOAL_PROMPT_REGISTRY if row["formatter"] == "revisions"
)
_GOAL_WAIT_LIFTED_NOTE_OPENS = tuple(
    str(row["template"])
    for row in GOAL_PROMPT_REGISTRY if row["formatter"] == "barrier_lift"
)
_GOAL_WAIT_LIFTED_NOTE_MATCHERS = tuple(
    _TemplateMatcher(str(row["template"]))
    for row in GOAL_PROMPT_REGISTRY if row["formatter"] == "barrier_lift_note"
)
# GoalManager appended the block directly to a continuation's terminal literal, so a revised
# continuation always contains terminal + block opening verbatim. Anchoring on that pair keeps a
# human suffix that merely quotes the revision header from being dropped as generated.
_REVISED_CONTINUATION_SEAMS = tuple(
    matcher.terminal + opening
    for kind, _template, matcher in _INJECTED_TURN_PATTERNS if kind == "goal"
    for opening in _REVISION_BLOCK_OPENS
)


def _unwrap_steer(suffix: str) -> str:
    if suffix.startswith(STEER_MARKER_OPEN + "\n") and suffix.endswith("\n" + STEER_MARKER_CLOSE):
        return suffix[len(STEER_MARKER_OPEN): -len(STEER_MARKER_CLOSE)].strip()
    if suffix.endswith("\n" + STEER_MARKER_CLOSE):
        # The boundary fell inside a steer that quoted it: the opening marker went with the
        # generated part, so drop the orphaned closing marker too.
        return suffix[: -len(STEER_MARKER_CLOSE)].strip()
    return suffix


def _user_after_injected_turn(content: str) -> str | None:
    """Drop a generated turn, preserving only a suffix after its exact formatter boundary.

    Every template is tried and the FURTHEST boundary wins. Templates share openings (all goal
    continuations, both ``/loop`` wakeups), so a payload can hold a complete rendering of a sibling
    template whose terminal ends inside the outer payload. The first match would end there and turn
    the rest of the payload into a "human" suffix. The outer prompt's own terminal always comes
    later, so the furthest boundary never calls generated text human, matching the last-copy rule.
    """
    match_kind = None
    marker_end = -1
    for kind, _template, matcher in _INJECTED_TURN_PATTERNS:
        end = matcher.match_end(content)
        if end > marker_end:
            match_kind, marker_end = kind, end
    if marker_end < 0:
        return content
    if match_kind == "goal" and any(seam in content for seam in _REVISED_CONTINUATION_SEAMS):
        # A revised continuation is generated through its end. Its revision block may also hold a
        # copy of the terminal, which would otherwise move the last-copy boundary inside it.
        return None

    suffix = content[marker_end:]
    if not suffix.strip():
        return None
    # The gateway's text merge joins a queued follow-up with a single newline, so one newline is a
    # real boundary after the template's terminal literal. Text glued to the terminal is not.
    if not suffix.startswith("\n"):
        return None
    suffix = suffix.strip()
    if match_kind == "goal" and (
        suffix.startswith(_GOAL_WAIT_LIFTED_NOTE_OPENS)
        or any(matcher.match_end(suffix) >= 0 for matcher in _GOAL_WAIT_LIFTED_NOTE_MATCHERS)
    ):
        # GoalManager appends one generated single-line barrier-lift note to an idle-woken continuation.
        # Exact historical notes are registered as templates; retain the existing opening fallback
        # for runtime outcome text that cannot be reconstructed from a transcript.
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
    if is_runtime_prompt(display_kind=display_kind, platform=platform):
        # Structured provenance is authoritative: no text boundary can make part of it human.
        return None
    text = (content or "").strip()
    # A recovery note can wrap another generated notice, so strip prefixes until none is left.
    while text:
        after = text_after_generated_prefix(text)
        if after == text:
            break
        text = (after or "").strip()
    return text or None


# GatewayInboundMixin._enrich_message_with_vision and the CLI vision enrichment emit this
# *pair* of notes. Use the second note as the boundary: descriptions can contain paragraphs,
# quoted brackets and even unbalanced brackets. Splitting on the first `]` loses human text.
# VisionMessagePrepMixin's no-advice data-URL note is deliberately absent: it rewrites only a
# deep copy of the API messages, and automatic recall reads the stored user message instead, whose
# multimodal parts flatten to the caption alone.
_IMAGE_RECALL_PREFIX = re.compile(
    r"\[The user (?:sent an image~ Here's what I can see:|"
    r"attached an image\. Here's what it contains:)\n(?P<description>.*?)\]\n"
    r"\[If you need a closer look, use vision_analyze with image_url: [^\n]+\](?=\n|$)",
    re.DOTALL,
)
# Names and paths can contain `] `; no producer puts human text on a note's own line, so a
# single-line note ends at its line's last `]`.
_IMAGE_REFERENCE_RECALL_PREFIX = re.compile(
    r"\[The user attached an image: [^\n]*\]\n"
    r"\[Examine it with the vision_analyze tool using image_url: [^\n]*\](?=\n|$)",
)
# Only known, single-line attachment metadata emitted by gateway/CLI producers, never arbitrary
# bracketed human prose. Speech-to-text's quoted transcript is intentionally NOT a metadata note.
_ATTACHMENT_RECALL_NOTE = re.compile(
    r"\[(?:User sent (?:an image|audio|a video|a file): "
    r"|The user sent (?:a (?:text )?document: '|an audio file attachment: '|a video attachment: '"
    r"|a voice message: |a voice message but it came through empty or inaudible "
    r"|an image but I couldn't quite see it this time "
    r"|an image but something went wrong when I tried to look at it)"
    r"|The user attached an image but (?:it couldn't be analyzed\. |analysis failed \()"
    r"|voice message could not be transcribed automatically; the audio is available at: )"
    r"[^\n]*\](?=\n|$)"
    # Bare fallback the gateway emits when the transcription module itself is missing.
    r"|\[voice message could not be transcribed\](?=\n|$)"
)


# Reply/trigger context is added outside media enrichment by
# GatewayInboundMixin._prepend_inbound_reply_context. It must be peeled before the
# attachment notes, or a reply caption still lands beyond the provider's query cap.
_REPLY_RECALL_PREFIX = re.compile(
    r'\[Replying to(?: your previous message)?: '
    r'"(?:\\.|[^"\\])*"\]\n\n',
)
_TRIGGER_RECALL_PREFIX = re.compile(
    r"\[Triggering message id: `[^\n]+` — use as `message_id` for reply/react/pin "
    r"via the discord tools\.\]\n\n",
)
_RECALL_CONTEXT_PREFIXES = (
    _TRIGGER_RECALL_PREFIX, _REPLY_RECALL_PREFIX, _IMAGE_RECALL_PREFIX,
    _IMAGE_REFERENCE_RECALL_PREFIX, _ATTACHMENT_RECALL_NOTE,
)


def _strip_recall_context(text: str) -> tuple[str, list[str]]:
    """Remove producer-owned attachment/reply scaffolding wherever it occurs in a turn.

    Synthetic recall deliberately keeps generated text, so it cannot use ``human_prompt_text``
    as a boundary first. Search rather than only matching at offset 0 because a generated notice
    may wrap the attachment enrichment. The meaningful image descriptions are retained separately
    for the image-only fallback.
    """
    descriptions: list[str] = []
    while text:
        matches = [match for pattern in _RECALL_CONTEXT_PREFIXES
                   if (match := pattern.search(text))]
        if not matches:
            break
        prefix = min(matches, key=lambda match: match.start())
        if "description" in prefix.re.groupindex:
            description = prefix.group("description").strip()
            if description:
                descriptions.append(description)
        text = (text[:prefix.start()] + text[prefix.end():]).strip()
    return text, descriptions


def _attachment_recall_text(text: str, *, include_synthetic: bool) -> str | None:
    """Prefer the request over prepended attachment context, with an image-only fallback.

    This affects recall only, not human_prompt_text (retention) or the model's original prompt.
    Attachment scaffolding is removed independently of synthetic-turn filtering, so enabling
    synthetic recall cannot move a human caption beyond the provider's query budget.
    """
    descriptions: list[str] = []
    while text:
        if not include_synthetic:
            human = human_prompt_text(text)
            if not human:
                # A generated suffix after an image description is not a human query. An
                # actually image-only turn has no suffix and may use the description fallback.
                return None if text else human_prompt_text("\n\n".join(descriptions))
            text = human
        stripped, found = _strip_recall_context(text)
        descriptions.extend(found)
        if stripped == text:
            break
        text = stripped
    if include_synthetic:
        human = human_prompt_text(text)
        if human and human != text:
            # Keep the generated text in the query, but put the actual human request first so
            # the provider's bounded input cannot discard it behind a long runtime notice.
            # The request already ends the text, so move it rather than repeat it.
            rest = text[:-len(human)].rstrip() if text.endswith(human) else text
            return f"{human}\n\n{rest}" if rest else human
    if text:
        return text
    fallback = "\n\n".join(descriptions)
    return fallback if include_synthetic else human_prompt_text(fallback)


def auto_recall_query(
    content: Optional[str], *, display_kind: Optional[str] = None, platform: Optional[str] = None,
    include_synthetic: bool = False,
) -> str:
    """The query for automatic memory recall this turn, or ``""`` to skip it.

    Synthetic turns recall nothing by default (``memory.recall_synthetic_turns``): their generated
    text is a poor semantic query and repeats verbatim across turns. A human suffix merged into one
    still recalls, keyed on that suffix alone. Attachment descriptions/path advice do not precede
    the human request in the query; meaningful image-only descriptions remain a fallback. Explicit
    memory tools, retained content and the model's prompt are unaffected.
    """
    text = (content or "").strip()
    query = text if include_synthetic else human_prompt_text(text, display_kind=display_kind, platform=platform)
    if query:
        query = _attachment_recall_text(query, include_synthetic=include_synthetic)
    return "" if not query or is_trivial_prompt(query) else query
