"""Persistent session goals — the Ralph loop for Hermes.

A goal is a free-form objective that stays active across turns. By default an auxiliary-model
judge decides whether it is satisfied; opt-in autonomy requires evidence-backed completion. The continuation prompt is a normal user message appended via
``run_conversation`` (no system-prompt mutation or toolset swap — prompt caching stays intact). Judge
failures are fail-OPEN (``continue``); the turn budget is the backstop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from collections.abc import Mapping

from agent.notice_style import ContextNotice
from hermes_cli._subprocess_compat import noninteractive_git_env
from hermes_cli.goals_contract_commands import GoalContractCommandsMixin
from hermes_cli.goals_legacy_prompts import (
    _LEGACY_CONTINUATION_PROMPT_TEMPLATE, _LEGACY_CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
    _LEGACY_CONTINUATION_REVISIONS_TEMPLATE, _LEGACY_CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE,
    _PRE_531_ADDITIVE_CONTRACT_TEMPLATE,
)
from hermes_cli.goals_contract_items import (
    _migrate_legacy_revision_requirements, contract_remove_aliases, goal_remove_usage, render_removable_contract_items,
)
from hermes_cli.goal_evidence import (
    JUDGE_EVIDENCE_BLOCK_TEMPLATE as JUDGE_EVIDENCE_BLOCK_TEMPLATE,
    _READ_ONLY_STATUS_CALL_RE as _READ_ONLY_STATUS_CALL_RE,
    _READ_ONLY_STATUS_TOOLS as _READ_ONLY_STATUS_TOOLS,
    _read_only_status_call as _read_only_status_call,
    _evidence_only_read_only_status,
    _evidence_scan_truncated_for_turn,
    _goal_progress_fingerprint,
    _render_evidence_block,
)
from hermes_cli.goals_revisions import GoalRevisionsMixin
from hermes_cli.goals_autonomy import (
    AUTONOMY_CONTINUATION_TEMPLATE, GoalAutonomyMixin, goal_autonomy_enabled, migrate_autonomous_goal,
    autonomous_verdict,
)
from hermes_cli.goals_status import GoalStatusMixin
from hermes_cli.goals_peers import GoalPeersMixin, parse_pending_peers, validate_peer_completion
from hermes_cli.lifecycle_snapshot import snapshot_control
from hermes_time import safe_strftime
from hermes_cli.heartbeat import SILENCE_MARKER, is_intentional_silence_response

logger = logging.getLogger(__name__)


# ── Constants & defaults ──────────────────────────────────────────────

DEFAULT_MAX_TURNS = 20
# Goal text is a compact outcome contract, not a work log.  Keep this rule in
# the core so the slash command and model-callable goal tool cannot drift.
GOAL_AUTHORING_MAX_CHARS = 600
# The cap is on the complete goal contract. Keep this alias for callers that
# imported the earlier name; a single field cannot exceed the total cap either.
GOAL_AUTHORING_CONTRACT_FIELD_MAX_CHARS = GOAL_AUTHORING_MAX_CHARS
_GOAL_AUTHORING_FIELDS = ("goal", "outcome", "verification", "constraints", "boundaries", "stop_when")
_GOAL_AUTHORING_NUMBERED_STEP_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*]\s*\d+[.)])\s+")
_GOAL_AUTHORING_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")
_GOAL_AUTHORING_STALE_REF_RES = (
    re.compile(r"(?i)(?<![\w])(?:pr|pull\s+request|issue|pull)\s*#?\s*\d+(?!\w)|(?:[\w./-]+)#\d+(?!\w)"),
    re.compile(r"(?i)(?<![\w])run(?:\s+id)?\s*#?\s*\d{6,}(?!\w)"),
    re.compile(r"(?<!\w)#\d+(?!\w)"),
)
_GOAL_AUTHORING_SHA_RE = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,40})(?![0-9A-Za-z])")
# Blanket limits contradict any mutating outcome. Effect-specific limits such as
# "no deploys" contradict only the same effect, and only when unqualified: "no
# deletions of user data" or "don't merge without approval" are real hard limits.
_GOAL_AUTHORING_BLANKET_LIMIT_RE = re.compile(
    r"(?i)\b(?:read[- ]only|no\s+changes?|without\s+changes?|do(?:es)?\s+not\s+change|don't\s+change)\b"
)
_GOAL_AUTHORING_LIMIT_NEGATION = r"(?:no|do(?:es)?\s+not|don't|never)\s+"
_GOAL_AUTHORING_LIMIT_END = r"\s*(?:[.;,!]|$)"
_GOAL_AUTHORING_SCOPED_LIMITS = {
    "relay": r"relay(?:s|ing)?",
    "send": r"send(?:s|ing)?",
    "delete": r"delet(?:e|es|ions?|ing)|remov(?:e|es|als?|ing)",
    "deploy": r"deploy(?:s|ing|ments?)?",
    "merge": r"merg(?:e|es|ing)",
}
_GOAL_AUTHORING_DURATION = r"\d+\s*[- ]?(?:hours?|hrs?|days?|weeks?|h|d|w)\b"
_GOAL_AUTHORING_METRIC_WINDOW_RE = re.compile(
    r"(?i)\b\d+[- ](?:hour|hr|day|week)s?[- ](?:error|rate|window|metric|budget|average|"
    r"p\d+|uptime|success|availability|latency|total|count|trend|report|summary)"
)
_GOAL_AUTHORING_LOOKBACK_RE = re.compile(r"(?i)\b(?:last|past|previous|prior|trailing|rolling)\b")


# A goal *requires* a mutation only when it commands one ("Delete the topic",
# "fix it and deploy") or names it as the end state ("the topic is deleted").
# Nouns and incidental past forms ("audit recent changes", "find which commit
# added the bug", "why the deploy failed") describe read-only work.
_GOAL_AUTHORING_REQUIRED_EFFECTS = {
    "delete": ("delete", "deleted"),
    "remove": ("remove", "removed"),
    "relay": ("relay", "relayed"),
    "send": ("send", "sent"),
    "deploy": ("deploy", "deployed"),
    "merge": ("merge", "merged"),
    "ship": ("ship", "shipped"),
    "install": ("install", "installed"),
    "publish": ("publish", "published"),
    "write": (r"write(?!\s+up\b)", "written"),
    "create": ("create", "created"),
    "restart": ("restart", "restarted"),
    "commit": ("commit", "committed"),
    "update": ("update", "updated"),
    "modify": ("modify", "modified"),
    "change": ("change", "changed"),
    "add": ("add", "added"),
    "move": ("move", "moved"),
    "rename": ("rename", "renamed"),
    "alter": ("alter", "altered"),
}
_GOAL_AUTHORING_CLAUSE_START = r"(?:^|[.;:!?\n]\s*|,\s*|\b(?:and|then)\s+)(?:please\s+)?"
# Present-tense end states only: "was deleted" or "were sent" report the past.
_GOAL_AUTHORING_END_STATE = r"\b(?:is|are|be|been|gets?)\s+(?:\w+ly\s+)?"
# A question or relative clause ("whether the topic is deleted", "files that are
# removed") asks about the state instead of requiring it.
_GOAL_AUTHORING_EMBEDDED_RE = re.compile(
    r"(?i)\b(?:whether|if|which|why|how|what|that|when|where|who|whose)\b"
)
_GOAL_AUTHORING_END_STATE_BOUNDARY_RE = re.compile(r"(?i),|\bthen\b")
_GOAL_AUTHORING_SCOPED_EFFECTS = {
    "relay": {"relay"}, "send": {"send"}, "delete": {"delete", "remove"},
    "deploy": {"deploy"}, "merge": {"merge"},
}


def goal_authoring_required_effects(text: str) -> set[str]:
    """Return mutation effects ``text`` commands or names as its end state."""
    text = text.strip()
    clauses = re.split(r"[.;:!?\n]", text)
    effects: set[str] = set()
    for effect, (base, participle) in _GOAL_AUTHORING_REQUIRED_EFFECTS.items():
        if re.search(rf"(?i){_GOAL_AUTHORING_CLAUSE_START}{base}\b", text):
            effects.add(effect)
            continue
        end_state = re.compile(rf"(?i){_GOAL_AUTHORING_END_STATE}{participle}\b")
        for clause in clauses:
            match = end_state.search(clause)
            while match:
                # A marker in an earlier subclause asks about that subclause;
                # a later clause may still independently require a mutation.
                boundaries = [boundary for boundary in _GOAL_AUTHORING_END_STATE_BOUNDARY_RE.finditer(
                    clause, 0, match.start()
                ) if not re.match(
                    r"(?i)\s*(?:is|are|be|been|gets?)\b", clause[boundary.end():]
                )]
                segment_start = boundaries[-1].end() if boundaries else 0
                if not _GOAL_AUTHORING_EMBEDDED_RE.search(clause[segment_start:match.start()]):
                    effects.add(effect)
                    break
                match = end_state.search(clause, match.end())
            if effect in effects:
                break
    return effects


def goal_authoring_readonly_contradiction(readonly: str, mutation: str) -> bool:
    """True when a limit forbids an effect the goal/outcome requires.

    Only commanded effects or required end states count; a read-only audit of
    "recent changes" or "why the deploy failed" is not a contradiction.
    """
    if not readonly or not mutation:
        return False
    required = goal_authoring_required_effects(mutation)
    if not required:
        return False
    if _GOAL_AUTHORING_BLANKET_LIMIT_RE.search(readonly):
        return True
    for name, limit in _GOAL_AUTHORING_SCOPED_LIMITS.items():
        bare = re.compile(rf"(?i)\b{_GOAL_AUTHORING_LIMIT_NEGATION}(?:{limit}){_GOAL_AUTHORING_LIMIT_END}")
        if bare.search(readonly) and required & _GOAL_AUTHORING_SCOPED_EFFECTS[name]:
            return True
    return False


def goal_authoring_fixed_wait(value: str) -> str | None:
    """Return the fixed elapsed-time wait in ``value``; measurement windows are evidence, not waits."""
    for match in _GOAL_AUTHORING_FIXED_WAIT_RE.finditer(value):
        text = match.group(0)
        if not re.search(_GOAL_AUTHORING_DURATION, text, re.IGNORECASE):
            return text
        if re.match(r"(?i)\s*(?:wait|after)\b", text):
            return text
        context = value[max(0, match.start() - 18):match.end() + 24]
        if re.search(r"(?i)\bwait\b", context):
            return text
        duration = re.search(_GOAL_AUTHORING_DURATION, text, re.IGNORECASE)
        absolute = match.start() + duration.start()
        if (_GOAL_AUTHORING_LOOKBACK_RE.search(context)
                or ("from" in context.lower() and "full" in context.lower())
                or _GOAL_AUTHORING_METRIC_WINDOW_RE.match(value, absolute)):
            continue
        return text
    return None
_GOAL_AUTHORING_RECEIPT_RE = re.compile(
    r"(?i)\b(?:read\s*back|receipt|logs?|tests?|checks?|review|closed|assigned|merged|passed|"
    r"fixed|restarted|deployed|verified)\b"
)
_GOAL_AUTHORING_FIXED_WAIT_RE = re.compile(
    r"(?i)(?:\bsoak(?:\s+test)?\b|\bobservation\s+window\b|\bstable\s+observation\b|"
    r"\b(?:stay(?:s|ed)?|remain(?:s|ed)?|hold(?:s|ing)?|maintain(?:s|ed)?|observe|wait|monitor(?:ing)?)\b"
    r".{0,60}?\b(?:for\s+)?\d+\s*[- ]?(?:hour|hr|day|week|h|d|w)s?\b|"
    r"\bafter\s+\d+\s*[- ]?(?:hour|hr|day|week|h|d|w)s?\b)"
)


@dataclass(frozen=True)
class GoalAuthoringFinding:
    """One deterministic authoring warning shared by every goal surface."""

    code: str
    field: str
    message: str
    fix: str

    def render(self) -> str:
        return f"{self.field}: {self.message} Fix: {self.fix}"


def _goal_authoring_values(goal: Any, contract: Any) -> dict[str, str]:
    if isinstance(contract, Mapping):
        data = contract
    elif contract is None:
        data = {}
    else:
        data = {field_name: getattr(contract, field_name, "") for field_name in _GOAL_AUTHORING_FIELDS[1:]}
    return {"goal": str(goal or "").strip(), **{
        field_name: str(data.get(field_name) or "").strip()
        for field_name in _GOAL_AUTHORING_FIELDS[1:]
    }}


def _goal_authoring_text_is_path(text: str, start: int, end: int) -> bool:
    """Do not call a plan/file path a stale identifier merely because it has digits."""
    token_start = max(text.rfind(" ", 0, start), text.rfind("\n", 0, start), text.rfind("\t", 0, start)) + 1
    token_end_candidates = [index for index in (text.find(" ", end), text.find("\n", end), text.find("\t", end)) if index >= 0]
    token_end = min(token_end_candidates, default=len(text))
    token = text[token_start:token_end].strip(".,;:!?()[]{}<>\"'")
    # owner/repo#N is a PR/issue reference, not a file path, even though it has a slash.
    if re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", token):
        return False
    return "/" in token or "\\" in token or bool(
        re.search(r"\.(?:md|markdown|txt|yaml|yml|json|toml)\b", token, re.IGNORECASE)
    )


def _goal_authoring_is_ordinal_hash(text: str, start: int) -> bool:
    """Allow ordinal rankings such as ``Ranked #1`` while rejecting bare receipts."""
    return bool(re.search(r"(?i)\b(?:ranked|top)\s*$", text[max(0, start - 24):start]))


def _goal_authoring_is_ordinary_identifier(text: str, start: int, end: int) -> bool:
    """Allow human identifiers such as ``cafe5678`` when clearly naming a table."""
    context = text[max(0, start - 32):min(len(text), end + 32)]
    return bool(re.search(r"(?i)\b(?:table|column|record|dataset)\b", context))


def goal_authoring_findings(
    goal: Any,
    contract: Any = None,
    *,
    previous_total: int | None = None,
    changed_fields: set[str] | None = None,
) -> list[GoalAuthoringFinding]:
    """Return stable warnings for compact, outcome-shaped goal contracts.

    ``changed_fields`` preserves legacy-revision behavior: an already oversized
    contract can receive a small unrelated fix, while a revision cannot make an
    in-cap contract oversized.  The slash command displays these findings; the
    model-callable tool rejects them.
    """
    values = _goal_authoring_values(goal, contract)
    checked = set(changed_fields or _GOAL_AUTHORING_FIELDS)
    findings: list[GoalAuthoringFinding] = []
    total = sum(len(values[name]) for name in _GOAL_AUTHORING_FIELDS)
    changed_total = sum(len(values[name]) for name in checked if name in values)
    if changed_fields is None:
        over_cap = total > GOAL_AUTHORING_MAX_CHARS
    else:
        over_cap = ((previous_total is None or previous_total <= GOAL_AUTHORING_MAX_CHARS)
                    and total > GOAL_AUTHORING_MAX_CHARS) or changed_total > GOAL_AUTHORING_MAX_CHARS
    if over_cap:
        field = "goal" if all(not values[name] for name in _GOAL_AUTHORING_FIELDS[1:]) else "contract"
        findings.append(GoalAuthoringFinding(
            "length", field, f"totals {total} characters; keep the outcome contract at or below "
            f"{GOAL_AUTHORING_MAX_CHARS}", "move steps, receipts, and ordering to the linked plan file",
        ))

    for field_name in _GOAL_AUTHORING_FIELDS:
        value = values[field_name]
        if field_name not in checked or not value:
            continue
        if len(value) > GOAL_AUTHORING_CONTRACT_FIELD_MAX_CHARS and field_name != "goal":
            findings.append(GoalAuthoringFinding(
                "field_length", field_name,
                f"is {len(value)} characters; maximum is {GOAL_AUTHORING_CONTRACT_FIELD_MAX_CHARS}",
                "keep this field outcome-shaped and move detail to the plan file",
            ))
        for pattern in _GOAL_AUTHORING_STALE_REF_RES:
            match = next((candidate for candidate in pattern.finditer(value)
                          if not (pattern is _GOAL_AUTHORING_STALE_REF_RES[-1]
                                  and _goal_authoring_is_ordinal_hash(value, candidate.start()))
                          and not _goal_authoring_text_is_path(value, candidate.start(), candidate.end())), None)
            if match:
                findings.append(GoalAuthoringFinding(
                    "stale_identifier", field_name,
                    f"contains stale-prone identifier {match.group(0)!r}",
                    "name the live outcome instead of a PR, issue, CI/run ID, or other transient receipt",
                ))
                break
        for match in _GOAL_AUTHORING_SHA_RE.finditer(value):
            token = match.group(1)
            if not (any(char.isdigit() for char in token) and any(char in "abcdef" for char in token)):
                continue
            if _goal_authoring_text_is_path(value, match.start(), match.end()) or _goal_authoring_is_ordinary_identifier(value, match.start(), match.end()):
                continue
            findings.append(GoalAuthoringFinding(
                "stale_identifier", field_name,
                f"contains commit-SHA-like identifier {token!r}",
                "remove the SHA and describe the live state it proves",
            ))
            break

    # Cross-field contradictions inspect the whole current contract whenever a
    # revision touches either side, so a goal change cannot turn an existing
    # read-only limit into a contradiction. Unrelated fixes (verification,
    # stop_when) to a legacy contradictory contract are not blocked.
    readonly = " ".join(values[name] for name in ("constraints", "boundaries"))
    mutation = " ".join(values[name] for name in ("goal", "outcome"))
    touches_contradiction = checked & {"goal", "outcome", "constraints", "boundaries"}
    if touches_contradiction and goal_authoring_readonly_contradiction(readonly, mutation):
        findings.append(GoalAuthoringFinding(
            "readonly_mutation", "constraints/boundaries",
            "declares read-only/no-change limits while the outcome requires a mutation",
            "replace the blanket read-only limit with the specific systems or effects that must not change",
        ))

    if "goal" in checked and _GOAL_AUTHORING_NUMBERED_STEP_RE.search(values["goal"]):
        findings.append(GoalAuthoringFinding(
            "numbered_steps", "goal", "contains a numbered step list",
            "state the outcome and move the steps to the plan file",
        ))
    for field_name in ("goal", "outcome", "verification"):
        value = values[field_name] if field_name in checked else ""
        if not value:
            continue
        wait_text = goal_authoring_fixed_wait(value)
        if wait_text:
            findings.append(GoalAuthoringFinding(
                "fixed_wait", field_name,
                f"uses a fixed waiting window ({wait_text})",
                "use a watcher or cron for elapsed-time waits and verify the resulting live state",
            ))

    verification = values["verification"] if "verification" in checked else ""
    if verification:
        checklist = bool(_GOAL_AUTHORING_BULLET_RE.search(verification) or
                          _GOAL_AUTHORING_NUMBERED_STEP_RE.search(verification) or
                          re.search(r"(?i)\b(?:step|item|check)\s*\d+", verification))
        separators = len(re.findall(r"[,;]", verification))
        if separators >= 3 and re.search(r"(?i)\b(?:and|or)\b", verification) and _GOAL_AUTHORING_RECEIPT_RE.search(verification):
            checklist = True
        if checklist:
            findings.append(GoalAuthoringFinding(
                "verification_checklist", "verification",
                "is a step/receipt checklist rather than one live end-state outcome",
                "state one observable result and put per-step checks in the plan file",
            ))
    return findings


def format_goal_authoring_warning(findings: list[GoalAuthoringFinding]) -> str:
    if not findings:
        return ""
    return "⚠ Goal authoring warning:\n" + "\n".join(f"- {finding.render()}" for finding in findings)
# ``goals.min_continuation_gap_seconds`` remains the configured lower bound between
# automatic continuations. The no-progress ladder adds its own escalating waits on top.
DEFAULT_MIN_CONTINUATION_GAP_SECONDS = 15 * 60
_MIN_CONTINUATION_GAP_REASON = "minimum gap between autonomous goal continuations"

# Stable opening used by every standing-goal continuation prompt. Consumers that need to
# distinguish synthetic turns (for example durable-memory filters) should match this exact
# generated boundary rather than the loose ``[Continuing toward`` text.
GOAL_CONTINUATION_PREFIX = "[Continuing toward your standing goal]\nGoal:"
GOAL_GATE_FAILED_PREFIX = "[Continuing toward your standing goal — a quality gate failed]\nGoal:"


def normalize_goal_max_turns(value: Any, default: int = DEFAULT_MAX_TURNS) -> int:
    """Normalize a goal budget; zero is the explicit unlimited sentinel."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed >= 0 else int(default)


def is_continuation_gap_wait(state: Optional["GoalState"]) -> bool:
    """True when the active barrier is only the minimum-gap hold between autonomous turns."""
    return bool(
        state is not None and state.waiting_until and state.waiting_on_pid is None
        and state.waiting_on_session is None and state.waiting_on_delegations == 0
        and state.waiting_reason == _MIN_CONTINUATION_GAP_REASON
    )


def normalize_goal_continuation_gap(value: Any, default: float = DEFAULT_MIN_CONTINUATION_GAP_SECONDS) -> float:
    """Normalize the minimum autonomous-continuation interval in seconds."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not (math.isfinite(parsed) and parsed >= 0):
        return float(default)
    return parsed


def _goal_budget_label(turns_used: int, max_turns: int) -> str:
    return f"{turns_used}/∞" if max_turns == 0 else f"{turns_used}/{max_turns}"


DEFAULT_JUDGE_TIMEOUT = 30.0
# Judge output budget. Reasoning models burn hidden-reasoning tokens before the visible one-line
# JSON verdict; 4096 covers every model live-tested.
DEFAULT_JUDGE_MAX_TOKENS = 4096
# Cap how much of the last response we send to the judge.
_JUDGE_RESPONSE_SNIPPET_CHARS = 4000
# A closeout usually ends with its evidence, so the judge also sees the response's tail.
_JUDGE_RESPONSE_TAIL_CHARS = 3000
# Judge failures fail open; no failure counter pauses a goal.
_EVIDENCE_MAX_ENTRIES = 8
_EVIDENCE_SCAN_ROWS = 120
_EVIDENCE_OUTPUT_CHARS = 800
_EVIDENCE_CALL_CHARS = 240
# Goal/memory bookkeeping cannot independently prove an outcome. Capability
# discovery, installation and readback can be the outcome itself, so keep their
# recorded results in both the recent ledger and the cited-evidence lookup.
_EVIDENCE_EXCLUDED_TOOLS = frozenset({
    "memory", "session_search", "todo", "todo_list", "clarify", "goal_set",
})
_EVIDENCE_EXCLUDED_TOOL_PREFIXES = ("hindsight_",)
# Passed to the SessionDB finders so exclusions apply before their row limits.
_EVIDENCE_EXCLUSION_KW = {"exclude_tools": tuple(sorted(_EVIDENCE_EXCLUDED_TOOLS)),
                          "exclude_prefixes": _EVIDENCE_EXCLUDED_TOOL_PREFIXES}

# Cited evidence: quoted identifiers are located in tool results since the goal started.
# Truncated ids can match by a sufficiently long prefix; commit URLs can match by hash.
_CITATION_MAX_NEEDLES = 24
_CITATION_MIN_CHARS = 6
_CITATION_MAX_CHARS = 200
_CITATION_CONTEXT_CHARS = 280
_CITATION_MAX_UNRESOLVED_SHOWN = 10
_CITATION_ROWS_PER_NEEDLE = 2
_CITATION_MAX_EXCERPTS = 32
_CITATION_BACKTICK_RE = re.compile(r"`([^`\n]{6,200})`")
_CITATION_QUOTED_RE = re.compile(r"[\"\u201c]([^\"\u201c\u201d`\n]{8,200})[\"\u201d]")
_CITATION_URL_RE = re.compile(r"https?://[^\s)\]>`\"']+")
_CITATION_COUNT_RE = re.compile(r"\b\d+ (?:tests? )?pass(?:ed|es)?\b")
_CITATION_TOKEN_RE = re.compile(r"\b(?:[0-9a-f]{7,64}|\d{8,}|[A-Za-z]+_[A-Za-z0-9]{8,}|[0-9]{8}T[0-9]{6}Z-[0-9a-f]+)\b")

# ``paused_reason`` prefix of the judge's BLOCKED auto-pause. It is the ONE pause kind a real
# user message may undo (see ``GoalManager.resume_for_user_input``), so it must be
# distinguishable from user/budget/judge-failure pauses that share ``status="paused"``.
_BLOCKED_PAUSE_PREFIX = "judge blocked: "
_LEGACY_BLOCKED_PAUSE_PREFIX = "judged unachievable: "


# Quality gates: deterministic shell commands that must pass before the judge may declare DONE. A
# failed gate short-circuits the judge — its output IS the continuation prompt, so the agent works
# on concrete evidence instead of a vibe check.
DEFAULT_GATE_TIMEOUT_SECONDS = 300
DEFAULT_GATE_MAX_RETRIES = 3
# Automatic no-progress waits are one quiet ladder. They are not a failure
# condition: a goal remains active and a real event resets the ladder.
_NO_PROGRESS_BACKOFF_S = (15 * 60, 60 * 60, 4 * 60 * 60, 24 * 60 * 60)
# Timed waits are not clamped to a polling window; this is only a sanity ceiling against
# a typo'd year. Events (user message, delivered notification) still pierce any wait.
_MAX_TIMED_WAIT_S = 30 * 24 * 60 * 60
DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS = 1
# Bounded tail of a failed gate's combined stdout/stderr fed back to the agent.
_GATE_OUTPUT_TAIL_CHARS = 3000


# Shared autonomous-wakeup reply contract. Goal continuations and /loop wakeups are machinery,
# not conversations: the user hears from them only when something new needs attention.
_UNCHANGED_AUTONOMOUS_RECHECK_RULE = (
    "A restart, update, or new release by itself is not news, and re-running a check that returns "
    "the same result as your last visible report counts as unchanged; reply with exactly "
    f"{SILENCE_MARKER} and nothing else. "
)


_AUTONOMOUS_REPLY_RULES = (
    "Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. "
    "If since your last visible update nothing new and material happened and nothing needs the "
    "user's action (for example, you are still waiting on an external party, a scheduled check, "
    "or a background process), reply with exactly "
    f"{SILENCE_MARKER} and nothing else. Never post status like \"no change\", \"still waiting\", "
    "plain acknowledgements, \"next check at\", or what you did not do.\n"
    "When something changed, report only the new fact or needed action briefly, in one or two short lines. "
    "The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that "
    "claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> "
    "block where the platform renders toggles; keep exact identifiers and output lines verbatim in backticks. "
    "Make a done claim visible again only when its substance changed because new evidence, "
    "new state, or a changed blocker appeared. If you already reported done and nothing material changed "
    "since that report, reply with exactly "
    f"{SILENCE_MARKER} and nothing else; the runtime will still re-check completion. "
    + _UNCHANGED_AUTONOMOUS_RECHECK_RULE
    + "The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need "
    "is unchanged, reply with exactly "
    f"{SILENCE_MARKER} and nothing else.\n"
)

CONTINUATION_PROMPT_TEMPLATE = (
    f"{GOAL_CONTINUATION_PREFIX} {{goal}}\n\n"
    + _AUTONOMOUS_REPLY_RULES
    + "Continue working toward this goal. Take the next concrete step. "
    "If you believe the goal is complete, state so explicitly, cite the proof "
    "(exact identifiers or output lines from tool results, in backticks), and stop. "
    "If you are blocked and need input from the user, say so clearly and stop."
)

# Revision history never enters a continuation: the current contract is the whole binding state.
# Every shipped revision-appendix wording stays in goals_prompt_registry for classification only.

# With a completion contract: the block tells the agent what "done" means, how to prove it, what
# not to break, scope, and when to stop — so it targets the verification surface.
CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE = (
    f"{GOAL_CONTINUATION_PREFIX} {{goal}}\n\n"
    "Completion contract:\n"
    "{contract_block}\n\n"
    + _AUTONOMOUS_REPLY_RULES
    + "Continue working toward the outcome above. Take the next concrete step. "
    "Stay within the stated boundaries and do not violate the constraints. "
    "Your method may change in the plan without changing the contract. goal_set "
    "action=revise can only add requirements. If an enrolled criterion is contradictory, "
    "obsolete, or already falsified by history, name the conflict and propose "
    "prospective verification through goal_set action=replace, preserving all "
    "unrelated requirements and hard limits. Use the latest real user instruction "
    "verbatim when it authorizes replacement, otherwise request the precise "
    "session-control approval card. Do not ask the user to clear the goal or "
    "compose a slash command. Explicit /goal remove <kind> <N> remains available "
    "for user-directed removal. Before "
    "claiming the goal is done, audit each Verification item against current state "
    "and end with an Evidence section that quotes exact identifiers or output lines "
    "from tool results in "
    "backticks (commit SHAs, run ids, URLs, `N passed` lines), so the runtime "
    "can locate them. If you hit the stated stop condition or are otherwise "
    "blocked and need user input, say so clearly and stop."
)


# With /subgoal criteria: surfaced verbatim to the agent and to the judge.
CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE = (
    f"{GOAL_CONTINUATION_PREFIX} {{goal}}\n\n"
    "Additional criteria the user added mid-loop:\n"
    "{subgoals_block}\n\n"
    + _AUTONOMOUS_REPLY_RULES
    + "Continue working toward the goal AND all additional criteria. Take "
    "the next concrete step. If you believe the goal and every "
    "additional criterion are complete, state so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly "
    "and stop."
)

# Fed back when a quality gate fails: bounded output is the evidence to repair against (no judge).
CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE = (
    f"{GOAL_GATE_FAILED_PREFIX} {{goal}}\n\n"
    "The quality gate command below must pass before this goal can be "
    "declared done, and it just failed (attempt {attempt}/{max_retries}):\n"
    "  $ {command}\n"
    "Exit code: {exit_code}\n"
    "Output (tail):\n"
    "```\n"
    "{output}\n"
    "```\n\n"
    "Fix the underlying problem so this gate passes, then re-run it to "
    "confirm. Do not declare the goal complete while any gate fails. If the "
    "gate itself is wrong or cannot pass, say so clearly and stop."
)

# All standing-goal continuation prompts are runtime injections, including the gate-failure form.
# Keep this registry shared by CLI, gateway, and TUI admission classifiers.
GOAL_CONTINUATION_PREFIXES = (
    "[Continuing toward your standing goal]",
    "[Continuing toward your standing goal — a quality gate failed]",
)


def is_goal_continuation_text(text: Any) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(GOAL_CONTINUATION_PREFIXES)


JUDGE_SYSTEM_PROMPT = (
    "You are a strict judge evaluating whether an autonomous agent has "
    "achieved a user's stated goal. You receive the goal text, the agent's "
    "most recent response, and — when present — tool results the runtime "
    "recorded while the goal was active and a list of background processes "
    "the agent has running. Recorded tool results come from the tool layer, "
    "not the agent's prose, so they count as concrete evidence even when the "
    "response only summarizes them. Decide one of four verdicts.\n\n"
    "DONE — the goal is fully satisfied:\n"
    "- The response explicitly confirms the goal was completed, OR\n"
    "- The response clearly shows the final deliverable was produced.\n"
    "On a silent autonomous recheck, the runtime may supply the agent's last visible "
    "report from this goal's lifetime. Decide whether that report claims completion "
    "and whether its citations and recorded evidence prove it; the runtime has not "
    "classified it as a completion claim. A newer report replaces every older one, "
    "including when it reports failed verification or unfinished work. Silence "
    "without a visible report adds no completion assertion.\n"
    "DONE requires the deliverable to actually exist. If the response only "
    "explains why the goal cannot be reached, the verdict is BLOCKED, not "
    "DONE.\n\n"
    "BLOCKED — the goal cannot be satisfied as stated:\n"
    "- The response explains the goal is genuinely unachievable (impossible, "
    "out of scope, no valid path to the deliverable), or refuses to "
    "fabricate a deliverable that cannot exist, OR\n"
    "- A constraint was breached, or an irreversible action the goal forbids "
    "has already happened, so the user must decide what happens next.\n"
    "If progress only needs user input (an answer, approval or decision), that is "
    "NOT BLOCKED: return WAIT with ``wait_on_user_message`` instead. Before "
    "choosing BLOCKED, prefer CONTINUE if the agent can investigate, adapt its "
    "method, or do independent authorized work. Return BLOCKED with the precise "
    "breach or reason in the reason; BLOCKED pauses the goal.\n"
    "When the block is an error the agent hit (an HTTP status, an API, "
    "sign-in or token failure), quote the error text verbatim in the reason "
    "and attribute it only to a provider, service or credential the response "
    "itself names. Never infer one the response does not name — an unnamed "
    "401 belongs to the model provider the agent was calling, not to some "
    "other service's token.\n\n"
    "WAIT — the goal is NOT done, but the next step is to wait for async "
    "work to finish rather than act again. Choose this ONLY when the agent's "
    "progress is genuinely gated on something running on its own:\n"
    "- A background process listed below is still running AND the response "
    "shows the agent is waiting on its result (e.g. a CI poller, build, "
    "test run, deploy). If the process has a session id, return it in "
    "``wait_on_session`` — that releases when the process exits OR its "
    "watch_patterns trigger fires (use this for a long-lived watcher that "
    "signals mid-run and may never exit). Otherwise return its pid in "
    "``wait_on_pid`` (releases on exit only).\n"
    "- The agent says it is rate-limited / backing off / must wait a fixed "
    "period — return seconds in ``wait_for_seconds``.\n"
    "- The agent has delegated subagents still running (stated below as "
    "active delegations) and the response says it is waiting on them with "
    "nothing else dispatchable — return ``wait_for_seconds``. Their results "
    "wake the agent on their own; re-poking it now only produces a status recap.\n"
    "- Progress is gated on work outside this session that is running or scheduled "
    "on its own — for example an external service, cron/watchdog, a stated next "
    "check time, or an elapsed-time hold — and the response says there is no "
    "actionable step now. Return ``wait_until`` as an ISO-8601 timestamp for a "
    "known deadline, or ``wait_for_seconds`` for a relative delay. There is no "
    "short ceiling on either form.\n"
    "- If the next step specifically needs a real user decision, return "
    '``{"verdict":"wait", "wait_on_user_message":true, ...}``; do not call this '
    "a permanent block.\n"
    "Picking WAIT parks the loop without burning a turn; it resumes "
    "automatically when the pid exits or the time elapses. Do NOT pick WAIT "
    "just because work remains — only when re-poking now would be pure "
    "busy-work because the agent can't progress until the async thing "
    "finishes.\n\n"
    "CONTINUE — not done, and there is a concrete next step the agent can "
    "take right now. This is the default when in doubt.\n\n"
    "Judge the end state, not the route. The agent may change its method, "
    "order, tools or plan as it learns; hold it to the outcome.\n\n"
    "Reply ONLY with a single JSON object on one line. Shapes:\n"
    '{"verdict": "done", "reason": "<one sentence>"}\n'
    '{"verdict": "blocked", "reason": "<one sentence>"}\n'
    '{"verdict": "continue", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_session": "<id>", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_pid": <int>, "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_for_seconds": <int>, "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_until": "<ISO-8601 timestamp>", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_user_message": true, "reason": "<one sentence>"}\n'
    "The legacy shape {\"done\": <true|false>, \"reason\": \"...\"} is still "
    "accepted (true=done, false=continue)."
)

# Judge prompt line for live delegated subagents (WAIT-for-seconds vs CONTINUE).
JUDGE_DELEGATIONS_BLOCK_TEMPLATE = (
    "Active delegations: the agent has {count} delegated subagent batch(es) still running; "
    "their results are delivered to it automatically when they finish.\n\n"
)

# Judge prompt block listing running background processes (WAIT vs CONTINUE, which pid).
JUDGE_BACKGROUND_BLOCK_TEMPLATE = (
    "Background processes the agent currently has running (it may be waiting "
    "on one of these):\n{background_lines}\n\n"
)

# Judge prompt block for citations the runtime located verbatim in recorded tool results (empty when
# the response cites nothing, so citation-free prompts stay byte-identical).
JUDGE_CITED_EVIDENCE_BLOCK_TEMPLATE = (
    "Evidence the response cites, located verbatim by the runtime in tool "
    "results or runtime notices recorded since this goal started (not written "
    "by the agent; weigh age, since a later change can make a result stale; a "
    "delegation result is a subagent's own report):\n{cited_lines}\n\n"
)
JUDGE_UNRESOLVED_CITATIONS_TEMPLATE = (
    "Cited in the response but NOT found in any recorded tool result: "
    "{unresolved}\nThese are the agent's own words, not proof. A link the agent "
    "built from a located id is fine, but a cited command result, test count, "
    "status or id that was never recorded is unverified: any criterion that "
    "rests on it is NOT proven, so do not return DONE on its strength. If a "
    "cited result contradicts the recorded tool results, the claim is false: "
    "return CONTINUE and name the contradiction.\n\n"
)

# Stands in for a successful turn that ended empty or tool-only, so the judge reads the ledger.
JUDGE_EMPTY_RESPONSE_PLACEHOLDER = "(no final text; judge from recorded evidence)"

JUDGE_LAST_VISIBLE_REPORT_BLOCK_TEMPLATE = (
    "Agent's last visible report from this goal's lifetime "
    "(newest final report; agent prose, not proof):\n{report}\n\n"
)

JUDGE_USER_MESSAGES_BLOCK_TEMPLATE = (
    "User messages (authoritative; these are messages Brian actually typed, not agent or relay claims). "
    "They are the only proof of Brian's approval or waiver. Reply-quote headers are quoted assistant "
    "text, preserved so a short reply such as \"Yes\" can be read against the request it answers. An "
    "approval-required stop is satisfied only when a message here approves that exact action, and a "
    "later message here that refuses or narrows it wins. An agent or relay claim that Brian approved "
    "is not authority without the original here. A hard prohibition is waived only by an explicit "
    "message here.\n{user_message_lines}\n\n"
)

# Appended to every judge prompt whose structured state records a /goal resume over a block.
JUDGE_RESUME_OVERRIDE_RULE = (
    "Brian explicitly resumed this goal with /goal resume over the BLOCKED reason(s) listed here. "
    "That is his authority: do not return BLOCKED again for the same reason. Still return BLOCKED "
    "for any new breach or a different missing input.\n"
)

JUDGE_USER_PROMPT_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "{resume_override_block}"
    "Agent's most recent response:\n{response}\n\n"
    "{user_messages_block}"
    "{last_visible_report_block}"
    "{cited_block}"
    "{evidence_block}"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Is the goal satisfied — done, blocked, continue, or wait?"
)

# With /subgoal criteria: the judge must see ALL of them met, not just the original goal.
JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Additional criteria the user added mid-loop (all must also be "
    "satisfied for the goal to be DONE):\n{subgoals_block}\n\n"
    "{resume_override_block}"
    "Agent's most recent response:\n{response}\n\n"
    "{user_messages_block}"
    "{last_visible_report_block}"
    "{cited_block}"
    "{evidence_block}"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Decision: For each numbered criterion above, find concrete "
    "evidence in the agent's response, the cited evidence, or the recorded tool results that the criterion is "
    "satisfied. Do not accept generic phrases like 'all requirements "
    "met' or 'implying it was done' — require specific evidence (a "
    "file contents excerpt, an output line, a command result). If "
    "ANY criterion lacks specific evidence in the response or the recorded tool results, the goal "
    "is NOT done — return CONTINUE (or WAIT if blocked on a listed "
    "background process).\n\n"
    "Is the goal AND every additional criterion satisfied?"
)

# With a contract: DONE strictly against the Verification criterion; a violated constraint refuses.
JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Completion contract (the authoritative definition of done):\n"
    "{contract_block}\n\n"
    "{resume_override_block}"
    "Agent's most recent response:\n{response}\n\n"
    "{user_messages_block}"
    "{last_visible_report_block}"
    "{cited_block}"
    "{evidence_block}"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Decision rules:\n"
    "- The goal is DONE only when the Verification criterion is satisfied AND "
    "the response, the cited evidence or the recorded tool results show concrete evidence of it "
    "(a command result, file contents excerpt, test/benchmark output) — not a "
    "claim like 'done' or 'all tests pass' with no supporting evidence.\n"
    "- Verification items no command can prove (a human review, a report to "
    "the user) are satisfied by the response stating them, unless the "
    "evidence contradicts it.\n"
    "- When the goal's boundaries or constraints forbid demonstrating a case live, recorded "
    "test evidence for that case satisfies the criterion if the tool output names the tests and "
    "includes a pass count.\n"
    "- Judge the end state, not the route: a different method, order or tool "
    "than the agent first planned is fine when the outcome holds.\n"
    "- A Constraint describing a state that must hold (e.g. no secret left "
    "published) blocks DONE only while it is currently violated; a breach that "
    "was remedied and verified no longer blocks. A breached prohibition on an "
    "irreversible action (e.g. a message sent to the wrong recipient) cannot be "
    "undone: return BLOCKED naming it so the user decides.\n"
    "- If the response shows the agent is waiting on a listed background "
    "process to satisfy the Verification criterion (e.g. CI is the "
    "verification and it's still running), return WAIT on that process "
    "instead of re-poking — re-poking now would be pure busy-work.\n"
    "- If the response explains the work is genuinely unachievable, or a "
    "constraint was breached, return BLOCKED with the reason describing it. "
    "If it hits the stated Stop condition and only needs user input, return "
    "WAIT with ``wait_on_user_message`` naming the decision.\n"
    "- Otherwise the goal is NOT done — CONTINUE.\n\n"
    "Is the goal satisfied per its completion contract — done, blocked, continue, or wait?"
)

# /goal draft: turn a plain objective into a reviewable contract (after Codex's "draft the goal").
DRAFT_CONTRACT_SYSTEM_PROMPT = (
    "You turn a user's plain-language objective into a structured completion "
    "contract for an autonomous coding agent. The contract has five fields:\n"
    "- outcome: the single end state that must be true when done\n"
    "- verification: the specific test / command / artifact that PROVES the "
    "outcome (must be concrete and checkable)\n"
    "- constraints: what must NOT change or regress\n"
    "- boundaries: which files, dirs, tools, or systems are in scope\n"
    "- stop_when: the condition under which the agent should stop and ask "
    "for human input instead of pushing on\n\n"
    "Infer sensible, specific values from the objective and any project "
    "context implied by it. Prefer concrete verification (a named test "
    "command, a build, a benchmark) over vague phrases. Keep each field to "
    "one or two sentences. If a field genuinely cannot be inferred, use an "
    "empty string for it.\n\n"
    "Reply ONLY with a single JSON object on one line:\n"
    '{"outcome": "...", "verification": "...", "constraints": "...", '
    '"boundaries": "...", "stop_when": "..."}'
)


# ── Completion contract ───────────────────────────────────────────────

# The five contract fields, in display order (after OpenAI Codex's "strong goal" guidance: what
# "done" means, how to prove it, what must not regress, what is in bounds, when to stop and ask).
# A bare free-form goal stays fully supported — empty fields are omitted from every prompt.
_CONTRACT_FIELDS = ("outcome", "verification", "constraints", "boundaries", "stop_when")

_CONTRACT_LABELS = {
    "outcome": "Outcome", "verification": "Verification", "constraints": "Constraints",
    "boundaries": "Boundaries", "stop_when": "Stop when blocked",
}

# Inline-input aliases the user may type before a value (`verify: tests pass`, `done when: ...`).
_CONTRACT_ALIASES = {
    "outcome": "outcome", "goal": "outcome", "done": "outcome", "done when": "outcome",
    "verification": "verification", "verify": "verification", "verified by": "verification",
    "evidence": "verification", "proof": "verification",
    "constraints": "constraints", "constraint": "constraints", "preserve": "constraints",
    "must not": "constraints", "do not change": "constraints",
    "boundaries": "boundaries", "boundary": "boundaries", "scope": "boundaries",
    "allowed": "boundaries", "files": "boundaries",
    "stop when": "stop_when", "stop_when": "stop_when", "blocked": "stop_when",
    "stop if blocked": "stop_when", "give up when": "stop_when",
}


@dataclass
class GoalContract:
    """Optional structured completion contract; empty fields are omitted everywhere."""
    outcome: str = ""
    verification: str = ""
    constraints: str = ""
    boundaries: str = ""
    stop_when: str = ""

    def is_empty(self) -> bool:
        return not any(getattr(self, f).strip() for f in _CONTRACT_FIELDS)

    def to_dict(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in _CONTRACT_FIELDS}

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> GoalContract:
        if not isinstance(data, dict):
            return cls()
        return cls(**{f: str(data.get(f) or "").strip() for f in _CONTRACT_FIELDS})

    def render_block(self) -> str:
        """Non-empty fields as a labelled block; empty contract → empty string."""
        return "\n".join(f"- {_CONTRACT_LABELS[f]}: {getattr(self, f).strip()}" for f in _CONTRACT_FIELDS if getattr(self, f).strip())


def parse_contract(text: str) -> tuple[str, GoalContract]:
    """Split user-typed goal text into a headline + contract from inline ``field: value`` lines.

    A headline without an explicit ``outcome:`` IS the outcome — it is not duplicated into the
    contract block (the goal text already carries it), so outcome stays empty in that case.
    """
    if not text:
        return "", GoalContract()
    headline_parts: list[str] = []
    fields: dict[str, list[str]] = {f: [] for f in _CONTRACT_FIELDS}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            prefix, _, value = line.partition(":")
            key = _CONTRACT_ALIASES.get(prefix.strip().lower())
            if key is not None and value.strip():
                fields[key].append(value.strip())
                continue
        headline_parts.append(line)
    contract = GoalContract(**{f: " ".join(v).strip() for f, v in fields.items()})
    return " ".join(headline_parts).strip(), contract


def _normalize_item_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _split_text_units(text: str) -> List[str]:
    """Split a text contract field into independently removable items."""
    items = (_normalize_item_text(part.strip().rstrip(";").strip())
             for part in re.split(r"\n+|(?<=[.!?;])(?=\s|$)", text or ""))
    # Filter after stripping separators so a bare ";" never becomes an empty numbered item.
    return [item for item in items if item]


def contract_items(field_name: str, value: Any) -> List[str]:
    """Return the canonical atomic items for one contract or subgoal field.

    Contract text is sentence/separator-delimited; each subgoal list entry is already
    atomic and must never be split into sentences. Item identity is exact after
    whitespace normalization: case, punctuation, and quoted text remain significant.
    """
    if field_name == "subgoals":
        values = value if isinstance(value, list) else [value]
        return [_normalize_item_text(item) for item in values if _normalize_item_text(item)]
    if field_name not in _CONTRACT_FIELDS:
        raise ValueError(f"unknown goal item field: {field_name}")
    values = value if isinstance(value, list) else [value]
    return [item for raw in values for item in _split_text_units(str(raw or ""))]


def _render_extra_criteria(subgoals: List[str]) -> str:
    return "\n".join(f"- Extra criterion {i}: {text}" for i, text in enumerate(subgoals, start=1))


# ── Quality gates ─────────────────────────────────────────────────────

@dataclass
class GoalGate:
    """A deterministic shell command that must pass before a goal can be done.

    Gates run at turn boundary BEFORE the LLM judge; a failing gate short-circuits judging and its
    bounded output becomes the continuation prompt.
    """
    command: str
    timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_GATE_MAX_RETRIES
    attempts: int = 0
    last_exit_code: Optional[int] = None
    last_output_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> GoalGate:
        if not isinstance(data, dict):
            return cls(command="")
        return cls(
            command=str(data.get("command") or ""),
            timeout_seconds=int(data.get("timeout_seconds") or DEFAULT_GATE_TIMEOUT_SECONDS),
            max_retries=int(data.get("max_retries") or DEFAULT_GATE_MAX_RETRIES),
            attempts=int(data.get("attempts") or 0),
            last_exit_code=(int(data["last_exit_code"]) if data.get("last_exit_code") is not None else None),
            last_output_tail=str(data.get("last_output_tail") or ""),
        )


def _gate_workspace() -> tuple[Optional[str], Optional[str]]:
    """``(cwd, refusal)`` for this check's gates. A multi-session backend's process directory is not
    the session's project, so gates run in the scoped session workspace (#125369). A declared
    workspace that is not a directory on this host (deleted, remote, container) is a refusal: a
    relative gate run anywhere else would check a different project and could pass a failing goal,
    and no agent turn can fix it, so the caller pauses instead of retrying.
    No declared workspace keeps the classic resolution (TERMINAL_CWD, else the launch directory)."""
    from agent.runtime_cwd import resolve_agent_cwd, scoped_session_cwd

    declared = scoped_session_cwd()
    if declared:
        path = Path(declared).expanduser()
        if path.is_dir():
            return str(path), None
        return None, (f"the session workspace {declared} is not a directory on this host, "
                      "and running gates anywhere else would check a different project")
    try:
        return str(resolve_agent_cwd()), None
    except OSError:
        return None, None  # deleted launch directory: subprocess reports it per gate


def run_gate(gate: GoalGate, *, cwd: Optional[str] = None) -> tuple[bool, int, str]:
    """Run one gate through the shell. Returns ``(passed, exit_code, output_tail)``; a timeout kills
    the process and counts as exit code -1."""
    try:
        # utf-8/replace: operator-configured output is arbitrary bytes; strict codepage decoding of
        # one unmappable byte (emoji/CJK on a non-UTF-8 Windows console) kills the reader thread and
        # the tail the agent needs arrives empty.
        proc = subprocess.run(
            gate.command, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(1, int(gate.timeout_seconds)), cwd=cwd or None,
            check=False,
        )
        combined = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode == 0, proc.returncode, combined[-_GATE_OUTPUT_TAIL_CHARS:]
    except subprocess.TimeoutExpired as exc:
        out = "".join(c if isinstance(c, str) else c.decode("utf-8", "replace") for c in (exc.stdout, exc.stderr) if c)
        return False, -1, (out + f"\n[gate timed out after {gate.timeout_seconds}s]")[-_GATE_OUTPUT_TAIL_CHARS:]
    except Exception as exc:
        return False, -1, f"[gate could not run: {type(exc).__name__}: {exc}]"


# ── Goal state ────────────────────────────────────────────────────────

@dataclass
class GoalState:
    """Serializable goal state stored per session."""
    goal: str
    status: str = "active"          # active | paused | done | cleared
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    created_at: float = 0.0
    last_turn_at: float = 0.0
    last_verdict: Optional[str] = None        # "done" | "blocked" | "continue" | "wait" | "skipped"
    last_reason: Optional[str] = None
    paused_reason: Optional[str] = None       # why we auto-paused (budget, etc.)
    # Who paused it: "agent", "user" or "system". With goals.autonomy the agent may resume only
    # its own pause. Never inferred from paused_reason text. Legacy rows load as "".
    paused_by: str = ""
    # Tracked for diagnostics only; judge outages fail open and never pause a goal.
    consecutive_parse_failures: int = 0
    consecutive_transport_failures: int = 0
    # User-added criteria (/subgoal). Both the judge and continuation prompts include them.
    subgoals: list[str] = field(default_factory=list)
    # Wait barrier (judge ``wait`` verdict or ``/goal wait``): parks the loop instead of re-poking the
    # agent into busy-work. pid → until exit; session → until that process_registry session's OWN
    # trigger fires (exit OR watch_patterns match — preferred for watchers that signal mid-run);
    # until → wall-clock deadline. While ANY is active evaluate_after_turn returns
    # should_continue=False without burning a turn; cleared lazily when satisfied or by unwait/pause/
    # resume/clear. Defaults empty so old state_meta rows load unchanged.
    waiting_on_pid: Optional[int] = None
    # Whether a PID target was live when this barrier was set. A target that was already
    # finished (or unknown) is stale state, not a fresh event wake when observed by an idle surface.
    waiting_on_pid_live_at_park: Optional[bool] = None
    waiting_on_session: Optional[str] = None
    # Whether a session target was live when this barrier was set. A target that was already
    # finished (or unknown) is stale state, not a fresh event wake when observed by an idle surface.
    waiting_on_session_live_at_park: Optional[bool] = None
    # Verified by the parent, independent of ephemeral process/time barriers.
    pending_peers: List[Dict[str, str]] = field(default_factory=list)
    waiting_on_user_message: bool = False
    waiting_until: float = 0.0
    # Live delegation batches when a timed WAIT was set because of them; the barrier lifts as soon
    # as that count drops (a batch returned), not only when the timer runs out.
    waiting_on_delegations: int = 0
    # Requested timed-wait duration, kept stable across judge re-parks so notice dedupe does not
    # depend on the new absolute deadline. Old rows default to zero and remain readable.
    waiting_seconds: int = 0
    waiting_reason: Optional[str] = None
    waiting_since: float = 0.0
    contract: GoalContract = field(default_factory=GoalContract)
    # /goal gate add <cmd>: ALL must pass before the judge may declare done.
    gates: List[GoalGate] = field(default_factory=list)
    # Every durable mutation gets a new token, including pause/resume with equal values.
    mutation_id: str = ""
    # Contract edits and completion events are an audit trail. Recorded hard-limit breaches
    # remain binding under autonomy, even after a replacement.
    revisions: List[Dict[str, Any]] = field(default_factory=list)
    # Stable identity of the last parked state announced to the user. This is durable so repeated
    # internal wakes and gateway restarts do not replay an unchanged wait notice.
    last_wait_notice_key: Optional[str] = None
    last_continuation_notice_key: Optional[str] = None
    # Deterministic busy-loop backstop. A fingerprint of recorded tool evidence is the progress
    # signal; repeated automatic CONTINUEs with no new evidence and the same reason are parked.
    consecutive_no_progress: int = 0
    # Escalation level for backoff parks within the current no-progress streak. Old rows default 0.
    backoff_level: int = 0
    last_progress_fingerprint: Optional[str] = None
    # Wall-clock timestamp of the last automatic continuation admitted for this goal. This is
    # separate from ``last_turn_at`` because user and external-event turns are allowed to pierce
    # the floor without turning the goal into a sub-minute automatic poller.
    last_automatic_continuation_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> GoalState:
        data = json.loads(raw)
        raw_subgoals = data.get("subgoals") or []
        ints = {k: int(data.get(k) or 0) for k in (
            "turns_used", "consecutive_parse_failures", "consecutive_transport_failures",
            "waiting_on_delegations", "waiting_seconds", "consecutive_no_progress", "backoff_level")}
        floats = {k: float(data.get(k) or 0.0) for k in (
            "created_at", "last_turn_at", "waiting_until", "waiting_since",
            "last_automatic_continuation_at")}
        return cls(
            goal=data.get("goal", ""),
            mutation_id=str(data.get("mutation_id") or ""),
            status=data.get("status", "active"),
            max_turns=normalize_goal_max_turns(data.get("max_turns", DEFAULT_MAX_TURNS)),
            last_verdict=data.get("last_verdict"),
            last_reason=data.get("last_reason"),
            paused_reason=data.get("paused_reason"),
            paused_by=str(data.get("paused_by") or ""),
            subgoals=contract_items("subgoals", raw_subgoals),
            waiting_on_pid=(int(data["waiting_on_pid"]) if data.get("waiting_on_pid") else None),
            waiting_on_pid_live_at_park=(
                bool(data["waiting_on_pid_live_at_park"])
                if data.get("waiting_on_pid_live_at_park") is not None else None
            ),
            waiting_on_session=(str(data["waiting_on_session"]) if data.get("waiting_on_session") else None),
            waiting_on_session_live_at_park=(
                bool(data["waiting_on_session_live_at_park"])
                if data.get("waiting_on_session_live_at_park") is not None else None
            ),
            pending_peers=parse_pending_peers(data.get("pending_peers", [])),
            waiting_on_user_message=bool(data.get("waiting_on_user_message")),
            waiting_reason=data.get("waiting_reason"),
            contract=GoalContract.from_dict(data.get("contract")),
            gates=[
                GoalGate.from_dict(g) for g in (data.get("gates") or [])
                if isinstance(g, dict) and str(g.get("command") or "").strip()
            ],
            revisions=[r for r in (data.get("revisions") or []) if isinstance(r, dict)]
            if isinstance(data.get("revisions"), list) else [],
            last_wait_notice_key=(str(data["last_wait_notice_key"]) if data.get("last_wait_notice_key") else None),
            last_continuation_notice_key=(str(data["last_continuation_notice_key"])
                                         if data.get("last_continuation_notice_key") else None),
            last_progress_fingerprint=(str(data["last_progress_fingerprint"])
                                       if data.get("last_progress_fingerprint") else None),
            **ints, **floats,
        )

    def has_contract(self) -> bool:
        return self.contract is not None and not self.contract.is_empty()

    def render_subgoals_block(self) -> str:
        """Numbered ``- N. text`` block; empty when there are no subgoals."""
        return "\n".join(
            f"- {i}. {text}" for i, text in enumerate(contract_items("subgoals", self.subgoals), start=1)
        )

    def _replacement_boundary(self) -> int:
        """Index of the latest user-authorized replacement revision, else -1."""
        return max(
            (i for i, revision in enumerate(self.revisions)
             if (revision.get("kind") == "set" and revision.get("authority") == "autonomy")
             or (revision.get("kind") == "replace" and revision.get("actor") == "user"
                 and revision.get("authority") in {"quote", "button", "session", "explicit_command"})),
            default=-1,
        )

    def resume_override_reasons(self) -> List[str]:
        """The block reasons a user-command resume waived since the latest replacement.

        Only the exact structured ``user_command`` field grants the waiver. Agent prose,
        revision reasons, and rendered prompt text cannot imitate it.
        """
        boundary = self._replacement_boundary()
        return [str(revision.get("reason") or "") for revision in self.revisions[boundary + 1:]
                if revision.get("user_command") == "/goal resume"]

    def has_resume_override(self) -> bool:
        """Whether this goal has a user-command resume waiver."""
        return bool(self.resume_override_reasons())

    def clear_wait(self, *, preserve_notice_key: bool = False) -> None:
        self.waiting_on_pid = None
        self.waiting_on_pid_live_at_park = None
        self.waiting_on_session = None
        self.waiting_on_session_live_at_park = None
        self.waiting_on_user_message = False
        self.waiting_until = 0.0
        self.waiting_on_delegations = 0
        self.waiting_seconds = 0
        self.waiting_reason = None
        self.waiting_since = 0.0
        if not preserve_notice_key:
            self.last_wait_notice_key = None
            self.last_continuation_notice_key = None


# ── Persistence (SessionDB state_meta) ────────────────────────────────

def _meta_key(session_id: str) -> str:
    return f"goal:{session_id}"


_DB_CACHE: dict[str, Any] = {}
_DB_BOOTSTRAP_LOCK = threading.Lock()
_DB_BOOTSTRAP_INFLIGHT: dict[str, threading.Event] = {}

# How long a loop-thread caller waits for an ALREADY-RUNNING bootstrap before degrading to None.
# Normal SessionDB init is ~10-100ms so a mid-bootstrap call usually picks the cached instance up;
# a contended init (locked state.db mid-migration) exceeds it and degrades. Far under the
# watchdog's probe window.
_DB_BOOTSTRAP_LOOP_WAIT_S = 0.25

# The call that STARTS the bootstrap (cold cache) waits this long instead. A fresh state.db init
# (schema DDL, FTS tables, first hermes_cli.config import) measures ~300ms warm and more on slow
# CI — well past 0.25s, which used to drop the first /goal write ("Goal set" but nothing
# persisted). Only the kick call pays this one-time stall; later calls keep the short window.
_DB_BOOTSTRAP_INIT_WAIT_S = 1.5


def _bootstrap_session_db(home: str, done: threading.Event) -> None:
    """Construct SessionDB off-loop and populate the cache (worker thread)."""
    try:
        db = _acquire_session_db(home)
    except Exception as exc:  # pragma: no cover
        logger.debug("GoalManager: background SessionDB() raised (%s)", exc)
        db = None
    with _DB_BOOTSTRAP_LOCK:
        if db is not None and home not in _DB_CACHE:
            _DB_CACHE[home] = db
            db = None
        _DB_BOOTSTRAP_INFLIGHT.pop(home, None)
    if db is not None:  # lost the race; drop our reference
        _release_session_db(db)
    done.set()


def _get_session_db() -> Optional[Any]:
    """Cached SessionDB per HERMES_HOME (profile switches pick the right DB); None on any failure.

    Never constructs SessionDB on an event-loop thread: a cache miss there kicks a one-shot background
    bootstrap and waits a bounded grace window (the kick call waits ``_DB_BOOTSTRAP_INIT_WAIT_S`` so a
    healthy cold init completes and the first write isn't dropped).
    """
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
    except Exception as exc:  # pragma: no cover
        logger.debug("GoalManager: SessionDB bootstrap failed (%s)", exc)
        return None

    cached = _DB_CACHE.get(home)
    if cached is not None and _registry_tore_down(cached):
        # ``hermes profile delete`` force-closes every handle under the profile home
        # (``hermes_state_registry.close_all_under``) before rmtree; a same-name recreate in this
        # process must acquire a fresh handle, not keep writing into the torn-down one.
        with _DB_BOOTSTRAP_LOCK:
            if _DB_CACHE.get(home) is cached:
                del _DB_CACHE[home]
        cached = None
    if cached is not None:
        return cached

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        on_loop_thread = False
    else:
        on_loop_thread = True

    if on_loop_thread:
        with _DB_BOOTSTRAP_LOCK:
            # Re-check under the lock: a bootstrap may have finished since the unlocked read.
            cached = _DB_CACHE.get(home)
            if cached is not None:
                return cached
            done = _DB_BOOTSTRAP_INFLIGHT.get(home)
            wait = _DB_BOOTSTRAP_LOOP_WAIT_S   # already running: brief grace window only
            if done is None:
                done = _DB_BOOTSTRAP_INFLIGHT[home] = threading.Event()
                threading.Thread(target=_bootstrap_session_db, args=(home, done), name="goals-sessiondb-bootstrap", daemon=True).start()
                wait = _DB_BOOTSTRAP_INIT_WAIT_S   # kick call pays the one-time init cost
        done.wait(wait)
        return _DB_CACHE.get(home)

    try:
        db = _acquire_session_db(home)
    except Exception as exc:  # pragma: no cover
        logger.debug("GoalManager: SessionDB() raised (%s)", exc)
        return None
    with _DB_BOOTSTRAP_LOCK:
        existing = _DB_CACHE.get(home)
        if existing is not None:
            # A concurrent bootstrap won the race; drop our reference so connections don't leak.
            _release_session_db(db)
            return existing
        _DB_CACHE[home] = db
    return db


def _acquire_session_db(home: str):
    """The registry's shared handle for ``home/state.db``. A bare ``SessionDB()`` here was a SECOND
    writer per profile beside the gateway's registry handle — its own token-writer thread and
    close-time checkpoint (the #90837 corruption shape), doubled under multiplexing."""
    from hermes_state_registry import acquire
    return acquire(Path(home) / "state.db")


def _release_session_db(db) -> None:
    from hermes_state_registry import release_or_close
    try:
        release_or_close(db)
    except Exception:
        pass


def _registry_tore_down(db) -> bool:
    """True once the registry force-closed *db* (``close_all`` / ``close_all_under`` clear the
    shared-owned flag at teardown); every handle cached here was acquired through the registry, so a
    cleared flag means the connection is gone and the cache entry is stale."""
    return getattr(db, "_shared_registry_owned", True) is False


def _warn_dropped_write(manager: str, kind: str, session_id: str) -> None:
    """WARN on a dropped state write — the reply already told the user the state was set. One shared
    message keeps goal, loop and heartbeat logs greppable as one bug class."""
    logger.warning(
        "%s: %s for %s not persisted — session DB unavailable "
        "(bootstrap window exceeded, in-memory state still active)",
        manager, kind, session_id,
    )


def _parse_goal_row(session_id: str, raw: str) -> GoalState:
    try:
        return GoalState.from_json(raw)
    except Exception as exc:
        logger.warning("GoalManager: could not parse stored goal for %s: %s", session_id, exc)
        raise GoalRowCorrupt(
            "existing goal could not be parsed; refusing to overwrite it. /goal clear discards it"
        ) from exc


def load_goal(session_id: str, *, cursor=None, db=None) -> Optional[GoalState]:
    """Load the goal, atomically upgrading legacy history-backed requirements once."""
    if not session_id:
        return None
    db = _get_session_db() if db is None else db
    if db is None:
        return None
    try:
        if cursor is not None:
            row = cursor.execute("SELECT value FROM state_meta WHERE key = ?", (_meta_key(session_id),)).fetchone()
            raw = row[0] if row else None
        else:
            raw = db.get_meta(_meta_key(session_id))
    except Exception as exc:
        if cursor is not None:
            raise
        logger.warning("GoalManager: get_meta failed for %s: %s", session_id, exc)
        raise GoalPersistenceError("existing goal could not be read; refusing to treat it as absent") from exc
    if not raw:
        return None
    state = _parse_goal_row(session_id, raw)
    migrated = _migrate_legacy_revision_requirements(state)
    if not (migrate_autonomous_goal(state) or migrated):
        return state

    # Session-control mutations already own the SessionDB write transaction. Re-entering
    # _execute_write here deadlocks on the non-reentrant writer lock; apply the migration to the
    # caller's cursor instead. The enclosing command CAS then commits migration + mutation
    # atomically.
    if cursor is not None:
        state.mutation_id = uuid.uuid4().hex
        cursor.execute("UPDATE state_meta SET value = ? WHERE key = ?",
                       (state.to_json(), _meta_key(session_id)))
        return state

    def _txn(conn) -> Optional[GoalState]:
        # Never overwrite a command or verdict that raced the initial read.
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (_meta_key(session_id),)).fetchone()
        if row is None or not row[0]:
            return None
        live = GoalState.from_json(row[0])
        migrated = _migrate_legacy_revision_requirements(live)
        if migrate_autonomous_goal(live) or migrated:
            live.mutation_id = uuid.uuid4().hex
            conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (live.to_json(), _meta_key(session_id)))
        return live

    try:
        return db._execute_write(_txn)
    except Exception as exc:
        # The row existed and the migration was computed successfully. Returning None here makes
        # GoalManager believe there is no goal, so a following /goal <text> can overwrite the
        # unrestored durable row. Keep the migrated in-memory state and mark it so ordinary
        # mutations retry the migration transaction; set() explicitly refuses to replace it.
        setattr(state, "_legacy_migration_pending", True)
        logger.warning("GoalManager: legacy requirement migration failed for %s: %s", session_id, exc, exc_info=True)
        return state


def save_goal(session_id: str, state: GoalState, *, cursor=None) -> None:
    """Persist a goal to SessionDB. No-op if DB unavailable."""
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        if cursor is not None:
            raise RuntimeError("session-control store unavailable")
        _warn_dropped_write("GoalManager", "goal", session_id)
        return
    validate_peer_completion(state)
    try:
        state.mutation_id = uuid.uuid4().hex
        if cursor is None:
            db.set_meta(_meta_key(session_id), state.to_json())
        else:
            db.set_meta(_meta_key(session_id), state.to_json(), cursor=cursor)
    except Exception as exc:
        if cursor is not None:
            raise
        logger.debug("GoalManager: set_meta failed: %s", exc)


def clear_goal_wait_if_since(session_id: str, waiting_since: float) -> Tuple[bool, Optional[GoalState]]:
    """Atomically clear the wait barrier iff the durable row is still the active wait parked at
    ``waiting_since``. Read, compare and write run in one ``BEGIN IMMEDIATE`` transaction, so a
    concurrent re-park, pause or clear (resumed turn, goal command) can never be overwritten by a
    stale snapshot. Returns ``(cleared, row_as_seen)``."""
    db = _get_session_db()
    if not session_id or db is None:
        return False, None
    key = _meta_key(session_id)

    def _txn(conn) -> Tuple[bool, Optional[GoalState]]:
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None or not row[0]:
            return False, None
        state = GoalState.from_json(row[0])
        has_wait = state.waiting_on_pid is not None or state.waiting_on_session is not None or getattr(state, "waiting_on_user_message", False) or state.waiting_until
        if state.status != "active" or not has_wait or state.waiting_since != waiting_since:
            return False, state
        state.clear_wait(preserve_notice_key=True)
        state.mutation_id = uuid.uuid4().hex
        conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (state.to_json(), key))
        return True, state

    try:
        return db._execute_write(_txn)
    except Exception as exc:
        logger.warning("GoalManager: conditional wait clear failed for %s: %s", session_id, exc)
        return False, None


def clear_goal(session_id: str) -> None:
    """Mark a goal cleared in the DB (preserved for audit, status=cleared)."""
    state = load_goal(session_id)
    if state is None:
        return
    state.status = "cleared"
    save_goal(session_id, state)


def migrate_goal_to_session(old_session_id: str, new_session_id: str, *, reason: str = "") -> bool:
    """Carry a persistent /goal from a parent session to its continuation. Best-effort, never raises
    (a failure here must not block compression). Returns True when a goal was migrated.

    Context compression rotates ``session_id`` to a fresh child session, but ``load_goal`` does a flat
    ``goal:<session_id>`` lookup with no parent-lineage walk — so an active goal silently dies at the
    compaction boundary (#33618). Copy the goal onto the new session and archive the old row as ``cleared``
    so exactly one active goal row exists per logical conversation (avoids the "two active goals" hazard of
    a pure copy).
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        state = load_goal(old_session_id)
        if state is None or state.status == "cleared":
            return False
        # Don't clobber a goal already set on the child (e.g. a resumed lineage).
        if load_goal(new_session_id) is not None:
            return False
        save_goal(new_session_id, state)
        # Archive the parent's row so it isn't double-counted as active.
        clear_goal(old_session_id)
        logger.debug("GoalManager: migrated goal %s -> %s (%s)", old_session_id, new_session_id, reason or "rotation")
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("GoalManager: goal migration failed: %s", exc)
        return False


# ── Judge ─────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "… [truncated]"


def _pid_alive(pid: int) -> bool:
    """Liveness via ``gateway.status._pid_exists`` (psutil + ctypes/POSIX fallback). Never uses
    ``os.kill(pid, 0)``: on Windows that routes to CTRL_C_EVENT and hard-kills the target's console
    group (bpo-14484)."""
    if not pid or pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(int(pid)))
    except Exception:
        pass
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        return False


def _async_delegation_completion_pending(session_id: str) -> bool:
    """True when a terminal async delegation still has an undelivered completion event."""
    try:
        from tools.async_delegation import get_durable_delegation

        row = get_durable_delegation(str(session_id))
        return bool(row and row.get("delivery_state") == "pending")
    except Exception:
        return False


def _session_waiting(session_id: str) -> bool:
    """True while the process_registry session is running and its trigger hasn't fired. Fail-safe:
    any import/registry error yields False so a stale barrier can never wedge the loop."""
    if not session_id:
        return False
    try:
        from tools.async_delegation import _LIVE_STATES, _records, _records_lock

        with _records_lock:
            delegation = _records.get(str(session_id))
        if delegation is not None and delegation.get("status") in _LIVE_STATES:
            return True
        if _async_delegation_completion_pending(str(session_id)):
            return True
    except Exception:
        pass
    try:
        from tools.process_registry import process_registry

        return bool(process_registry.is_session_waiting(session_id))
    except Exception:
        return False


def _process_outcome(session_id: str) -> Optional[Dict[str, Any]]:
    """Terminal facts for a process_registry session: the live registry first, then the durable
    receipt under ``logs/process-results``. None when neither knows it (outcome unknown)."""
    try:
        from tools.async_delegation import _LIVE_STATES, _records, _records_lock

        with _records_lock:
            delegation = _records.get(str(session_id))
        if delegation is not None:
            if (delegation.get("status") in _LIVE_STATES
                    or _async_delegation_completion_pending(str(session_id))):
                return {"running": True, "delegation": True}
            return {"delegation": True, "state": delegation.get("status")}
    except Exception:
        pass
    try:
        from tools.process_registry import process_registry

        session = process_registry.get(session_id)
        if session is not None and session.id == session_id:
            if not session.exited:
                return {"running": True}
            return {"exit_code": session.exit_code, "completion_reason": session.completion_reason,
                    "termination_source": session.termination_source}
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "logs" / "process-results" / f"{session_id}.json"
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        if record.get("completion_reason") == "cut_pending":
            return None
        return {k: record.get(k) for k in ("exit_code", "completion_reason", "termination_source")}
    except Exception:
        return None


# An idle surface defers this long after a tracked notify_on_complete process exits, so its own
# completion turn (which re-judges the goal with the output in hand) wins instead of racing it.
_COMPLETION_NOTICE_GRACE_S = 120.0


def _completion_notice_pending(session_id: str) -> bool:
    """True while THIS process still holds a recently exited notify_on_complete session whose
    completion turn has not had time to run. A process killed by a previous gateway is absent from
    the live registry, so it never defers."""
    try:
        from tools.process_registry import process_registry

        with process_registry._lock:
            session = process_registry._finished.get(session_id)
        return bool(session is not None and session.notify_on_complete
                    and not process_registry.is_completion_consumed(session_id)
                    and time.time() - (session.exited_at or 0.0) < _COMPLETION_NOTICE_GRACE_S)
    except Exception:
        return False


# Opens every barrier-lift note. The note is generated, so memory gates drop it with the
# continuation it is appended to (agent.synthetic_prompt).
GOAL_WAIT_LIFTED_NOTE_OPEN = "[Goal wait lifted: "


def _barrier_lift_note(state: Optional["GoalState"]) -> str:
    """One factual line appended to an idle-woken continuation, so the agent does not assume the
    awaited work succeeded. Never asks for a rerun: interrupted work may have side effects."""
    if state is None:
        return ""
    if state.waiting_on_session:
        sid = state.waiting_on_session
        outcome = _process_outcome(sid)
        if outcome is None:
            return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}background process {sid} is no longer tracked and its outcome "
                    "is unknown. Check its output or artifacts and verify the real state before rerunning; "
                    "do not assume it succeeded.]")
        if outcome.get("running"):
            return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}background process {sid} is still running after "
                    "Check its progress before waiting again.]")
        if outcome.get("completion_reason") == "killed":
            status = ("stopped" if outcome.get("termination_source") in {"kill_all", "gateway_shutdown"}
                      else "was killed by an explicit kill")
            return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}background process {sid} {status} before it finished "
                    f"(exit {outcome.get('exit_code')}). Its result is incomplete; check it with the process "
                    "tool and verify the real state before deciding whether to rerun that work.]")
        return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}background process {sid} finished "
                f"({outcome.get('completion_reason') or 'exited'}, exit {outcome.get('exit_code')}). "
                "Read its output with the process tool before continuing.]")
    if state.waiting_on_pid:
        if _pid_alive(state.waiting_on_pid):
            return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}pid {state.waiting_on_pid} is still running after "
                    "Check its progress before waiting again.]")
        return f"{GOAL_WAIT_LIFTED_NOTE_OPEN}pid {state.waiting_on_pid} has exited. Verify its result before continuing.]"
    return ""


def list_parked_goals() -> List[Tuple[str, "GoalState"]]:
    """``[(session_id, GoalState)]`` for every ACTIVE goal carrying a wait barrier in the current
    profile's store; ``[]`` on any DB error. Used by the gateway's idle wakeup ticker."""
    db = _get_session_db()
    if db is None:
        return []
    out: List[Tuple[str, GoalState]] = []
    try:
        rows = db.list_meta_prefix("goal:")
    except Exception as exc:
        logger.debug("GoalManager: list_meta_prefix failed: %s", exc)
        return []
    for key, raw in rows:
        if not raw or ('"active"' not in raw and not goal_autonomy_enabled()):
            continue
        try:
            state = load_goal(key[len("goal:"):]) if goal_autonomy_enabled() else GoalState.from_json(raw)
        except Exception:
            continue
        if state is not None and state.status == "active" and (
                state.waiting_on_pid is not None or state.waiting_on_session is not None or getattr(state, "waiting_on_user_message", False) or state.waiting_until):
            out.append((key[len("goal:"):], state))
    return out


def store_has_parked_goal(db: Any) -> bool:
    """True when *db* holds an active goal with a wait barrier. Propagates read errors so an idle
    gate can tell "empty" from "unavailable" (callers fail open)."""
    for _key, raw in db.list_meta_prefix("goal:"):
        if raw and ('"active"' in raw or goal_autonomy_enabled()):
            try:
                state = GoalState.from_json(raw)
            except Exception:
                return True
            if goal_autonomy_enabled() and migrate_autonomous_goal(state):
                return True
            if state.status == "active" and (
                    state.waiting_on_pid is not None or state.waiting_on_session is not None or getattr(state, "waiting_on_user_message", False) or state.waiting_until):
                return True
    return False


_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _goal_judge_setting(key: str, default, cast):
    """Resolve ``auxiliary.goal_judge.<key>``; non-positive/garbage falls back to ``default``
    rather than crashing the loop. ``load_config()`` is cached on (mtime, size) so this is cheap."""
    try:
        from hermes_cli.config import load_config

        value = cast((load_config().get("auxiliary") or {}).get("goal_judge", {}).get(key, default))
        if value > 0:
            return value
    except Exception:
        pass
    return default


def _goal_judge_max_tokens() -> int:
    return _goal_judge_setting("max_tokens", DEFAULT_JUDGE_MAX_TOKENS, int)


def _goal_judge_timeout() -> float:
    return _goal_judge_setting("timeout", DEFAULT_JUDGE_TIMEOUT, float)


def _extract_json_object(raw: str) -> Optional[dict[str, Any]]:
    """Best-effort: strip code fences, parse the blob, else pull the first ``{...}`` out."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")   # peel off leading json/JSON tag
        if nl != -1:
            text = text[nl + 1:]
    try:
        data = json.loads(text)
    except Exception:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return value is True


# BLOCKED reasons that are a real stop rather than a wait for the user's answer.
_BLOCKED_PAUSE_RE = re.compile(
    r"\b(?:breach\w*|violat\w*|irreversibl\w*|impossible|"
    r"unachievable|must never|should not have|cannot be (?:achieved|done|completed|satisfied|undone)|"
    r"no longer (?:exists|possible)|already (?:sent|deleted|merged|published|posted|paid|pushed|removed)|"
    r"nothing (?:left|more) to try|no remaining (?:options|approaches))\b"
    # A completed action next to a prohibition is a breach; the prohibition alone is not.
    r"|\b(?:merged|sent|deleted|published|posted|paid|pushed|removed|dropped|wiped)\b[^.;]{0,80}"
    r"\b(?:forbid\w*|prohibit\w*|constraints?|must not)\b",
    re.I,
)
# BLOCKED reasons that only need the user's answer, approval or decision.
_BLOCKED_ON_USER_RE = re.compile(
    r"\b(?:brian|user|you|owner|operator)\b|\b(?:approv\w*|permission|sign-?off|decision|decide|"
    r"confirm\w*|answer|input|credentials?|login|consent)\b",
    re.I,
)

def _parse_wait_until(raw: str) -> Optional[float]:
    """Epoch for an ISO-8601 ``wait_until``. A timestamp without an offset is the host's local
    wall-clock time (the judge prompt shows local time), not UTC."""
    try:
        when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.astimezone()  # interpret naive as local time
    return when.timestamp()


def _parse_judge_response(raw: str) -> Tuple[str, str, bool, Optional[Dict[str, Any]]]:
    """Parse the judge's reply, fail-open. Returns ``(verdict, reason, parse_failed, wait_directive)``.

    ``wait_directive`` is ``{"session_id"}`` / ``{"pid"}`` / ``{"seconds"}`` / ``{"until"}``
    / ``{"user_message": True}`` for a wait verdict; a wait with no target is downgraded to
    ``continue``.
    the legacy ``{"done": <bool>}`` shape.
    """
    if not raw:
        return "continue", "judge returned empty response", True, None
    data = _extract_json_object(raw)
    if data is None:
        return "continue", f"judge reply was not JSON: {_truncate(raw, 200)!r}", True, None

    reason = str(data.get("reason") or "").strip() or "no reason provided"
    verdict_raw = data.get("verdict")
    if isinstance(verdict_raw, str):
        verdict = verdict_raw.strip().lower()
    else:
        done_val = data.get("done")
        done = done_val.strip().lower() in {"true", "yes", "1", "done"} if isinstance(done_val, str) else bool(done_val)
        verdict = "done" if done else "continue"
    if verdict not in {"done", "blocked", "continue", "wait"}:
        verdict = "continue"
    if verdict != "wait":
        return verdict, reason, False, None

    def _first_int(*keys: str) -> Optional[int]:
        for k in keys:
            try:
                iv = int(data[k]) if data.get(k) is not None else 0
            except (TypeError, ValueError):
                continue
            if iv > 0:
                return iv
        return None

    # Prefer session (releases on the process's own trigger), then pid (exit only), then seconds.
    sess = data.get("wait_on_session") or data.get("session_id") or data.get("wait_session")
    if isinstance(sess, str) and sess.strip():
        return "wait", reason, False, {"session_id": sess.strip()}
    pid = _first_int("wait_on_pid", "pid", "wait_pid")
    if pid is not None:
        return "wait", reason, False, {"pid": pid}
    until = data.get("wait_until")
    if isinstance(until, (int, float)) and float(until) > time.time():
        return "wait", reason, False, {"until": float(until)}
    if isinstance(until, str) and until.strip():
        epoch = _parse_wait_until(until)
        if epoch is not None and epoch > time.time():
            return "wait", reason, False, {"until": epoch}
    seconds = _first_int("wait_for_seconds", "seconds", "wait_seconds")
    if seconds is not None:
        return "wait", reason, False, {"seconds": seconds}
    if verdict == "wait" and _truthy(data.get("wait_on_user_message")):
        return "wait", reason, False, {"user_message": True}
    return "continue", f"{reason} (wait verdict had no target — continuing)", False, None


def _render_background_block(background_processes: Optional[list[dict[str, Any]]]) -> str:
    """Render RUNNING ``process_registry.list_sessions()`` entries for the judge prompt. Empty string
    when nothing is running, so the prompt stays byte-identical to the no-background case."""
    lines: list[str] = []
    for p in background_processes or []:
        if not isinstance(p, dict) or p.get("status") == "exited" or not p.get("pid"):
            continue
        cmd = _truncate(str(p.get("command") or "").replace("\n", " ").strip(), 120)
        tail = _truncate(str(p.get("output_preview") or "").replace("\n", " ").strip(), 120)
        line = f"- pid {p['pid']}"
        if p.get("session_id"):
            line += f" / session {p['session_id']}"
        line += f": {cmd}"
        if p.get("uptime_seconds") is not None:
            line += f" (running {p['uptime_seconds']}s)"
        # Surface the process's own trigger so the judge can wait on a mid-run signal, not just exit.
        wps = p.get("watch_patterns")
        if wps:
            hit = " [already matched]" if p.get("watch_hit") else ""
            line += f" | watch_patterns={wps}{hit}"
        elif p.get("notify_on_complete"):
            line += " | notify_on_complete"
        if tail:
            line += f" | recent output: {tail}"
        lines.append(line)
    if not lines:
        return ""
    return JUDGE_BACKGROUND_BLOCK_TEMPLATE.format(background_lines="\n".join(lines))


def _evidence_tool_excluded(name: str) -> bool:
    return name in _EVIDENCE_EXCLUDED_TOOLS or name.startswith(_EVIDENCE_EXCLUDED_TOOL_PREFIXES)


def _one_line(text: Any, limit: int) -> str:
    return _truncate(" ".join(str(text or "").split()), limit)


def _tail(text: str, limit: int) -> str:
    """Last ``limit`` chars: command verdicts (exit codes, pass/fail summaries) sit at the end."""
    return text if len(text) <= limit else "[…] " + text[-limit:]


class _EvidenceRows(list):
    """Evidence entries plus whether the bounded SessionDB scan omitted rows."""

    def __init__(self, entries: List[Dict[str, Any]], *, truncated: bool = False):
        super().__init__(entries)
        self.truncated = truncated


def collect_goal_evidence(session_id: Optional[str], since: float = 0.0, *,
                          max_entries: Optional[int] = _EVIDENCE_MAX_ENTRIES) -> List[Dict[str, Any]]:
    """Recent tool results from ``session_id`` recorded at or after ``since`` (oldest first).

    Each entry is ``{"tool", "call", "output", "timestamp"}``: the tool name, its arguments (from the
    matching assistant tool call), and a secret-redacted tail of the recorded output. Fail-safe: any
    error yields ``[]`` so the judge falls back to the response alone.
    """
    if not session_id:
        return []
    db = _get_session_db()
    if db is None:
        return []
    try:
        rows = db.get_messages(session_id, limit=_EVIDENCE_SCAN_ROWS + 1, latest=True)
    except Exception as exc:
        logger.debug("goal evidence: message read failed: %s", exc)
        return []
    scan_truncated = len(rows) > _EVIDENCE_SCAN_ROWS
    if scan_truncated:
        rows = rows[-_EVIDENCE_SCAN_ROWS:]
    calls: Dict[str, Tuple[str, str]] = {}
    for row in rows:
        for call in (row.get("tool_calls") or []) if row.get("role") == "assistant" else []:
            try:
                fn = call.get("function") or {}
                calls[str(call.get("id") or "")] = (str(fn.get("name") or ""), str(fn.get("arguments") or ""))
            except Exception:
                continue
    try:
        from agent.redact import redact_sensitive_text
    except Exception:  # pragma: no cover - redaction module is part of the runtime
        redact_sensitive_text = None
    entries: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("role") != "tool" or float(row.get("timestamp") or 0.0) < since:
            continue
        name, args = calls.get(str(row.get("tool_call_id") or ""), (str(row.get("tool_name") or ""), ""))
        name = name or str(row.get("tool_name") or "") or "tool"
        if _evidence_tool_excluded(name):
            continue
        content = row.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        call_text, output = _one_line(args, _EVIDENCE_CALL_CHARS), _tail(content.strip(), _EVIDENCE_OUTPUT_CHARS)
        if redact_sensitive_text is not None:
            call_text = redact_sensitive_text(call_text, force=True)
            output = redact_sensitive_text(output, force=True)
        entries.append({"tool": name, "call": call_text, "output": output,
                        "timestamp": float(row.get("timestamp") or 0.0)})
    selected = entries if max_entries is None else entries[-max_entries:]
    return _EvidenceRows(selected, truncated=scan_truncated)





def _evidence_for_turn(evidence: Optional[List[Dict[str, Any]]], previous_turn_at: float) -> List[Dict[str, Any]]:
    """Keep only evidence recorded since the previous completed turn."""
    rows = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        try:
            timestamp = float(item.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        if previous_turn_at <= 0.0 or timestamp > previous_turn_at:
            rows.append(item)
    return rows


def _judge_response_window(text: str) -> str:
    """Head plus tail of a long response: the summary leads and the evidence section usually closes."""
    text = text or ""
    head, tail = _JUDGE_RESPONSE_SNIPPET_CHARS, _JUDGE_RESPONSE_TAIL_CHARS
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n… [{omitted} chars omitted] …\n{text[-tail:]}"


def extract_citations(text: str) -> List[str]:
    """Exact identifiers a response cites: backtick spans, quoted strings, URLs, SHAs and long ids.

    Ordered by first appearance, deduplicated, bounded; the closing evidence section is scanned
    first so a long response's proof survives the cap."""
    text = text or ""
    hits: List[Tuple[int, str]] = []
    for regex, group in ((_CITATION_BACKTICK_RE, 1), (_CITATION_QUOTED_RE, 1),
                         (_CITATION_URL_RE, 0), (_CITATION_TOKEN_RE, 0), (_CITATION_COUNT_RE, 0)):
        hits += [(m.start(group), m.group(group)) for m in regex.finditer(text)]
    found: List[str] = []
    # Latest first: a closeout's Evidence section sits at the end, so it survives the cap.
    for _pos, value in sorted(hits, key=lambda h: -h[0]):
        value = value.strip()
        # Keep a deliberate ASCII ellipsis inside a quoted identifier; strip sentence punctuation.
        if not value.endswith("..."):
            value = value.strip(".,;:")
        # Bare words ("independent") match anything; require a digit, a symbol or a phrase.
        specific = any(ch.isdigit() or ch in "=:/._-#@ " for ch in value)
        if specific and _CITATION_MIN_CHARS <= len(value) <= _CITATION_MAX_CHARS and value not in found:
            found.append(value)
    return found[:_CITATION_MAX_NEEDLES]


def _citation_variants(needle: str) -> List[str]:
    """Lookups for an exact citation, a key/value citation, or a safe shortened shape.

    An ellipsis needs an eight-character prefix; commit URLs can fall back to their
    7–40 digit hex hash. The returned variant must occur in recorded evidence."""
    variants = [needle]
    if '"' in needle:
        # Tool results are stored as JSON, so quotes inside nested output are escaped.
        variants.append(needle.replace('"', '\\"'))
    for sep in ("=", ": "):
        if sep in needle:
            value = needle.split(sep, 1)[1].strip().strip("'\"")
            if len(value) >= _CITATION_MIN_CHARS and value not in variants:
                variants.append(value)
    if needle.endswith(("…", "...")):
        prefix = needle[:-1] if needle.endswith("…") else needle[:-3]
        # A shorter prefix is too ambiguous to establish evidence, even if the literal ellipsis
        # happens to appear in a result.
        return [prefix] if len(prefix) >= 8 else []
    commit = re.search(r"/commit/([0-9a-fA-F]{7,40})$", needle)
    if needle.startswith(("https://", "http://")) and commit:
        variants.append(commit.group(1))
    return variants


# Runtime-written user-role notices that carry results: a subagent's report or a process exit.
_RUNTIME_NOTICE_LABELS = (
    ("[ASYNC DELEGATION", "delegation result (subagent's report)"),
    ("[IMPORTANT: Background process", "background process notice"),
)


# display_kind values only the runtime writes on the user-role rows it injects. "hidden" is not
# among them: clients may submit hidden prompts, so a hidden row counts only with the runtime's
# delegation-delivery identity (see _runtime_notice_label).
_RUNTIME_NOTICE_KINDS = frozenset({"internal_notification", "async_delegation_complete", "process_complete"})
# display_kind values of user-role rows the person typed: none, or a mid-turn /steer message.
_USER_TYPED_KINDS = frozenset({"", "steer"})


def _runtime_notice_label(row: Dict[str, Any]) -> str:
    """Label for a runtime-delivered notice row, or "" when its provenance is not runtime-owned.

    The persisted ``display_kind`` authenticates the row; the text prefix only picks the label.
    A user message that merely starts with the same words is typed input, never evidence."""
    kind = str(row.get("display_kind") or "")
    meta = row.get("display_metadata") if isinstance(row.get("display_metadata"), dict) else {}
    # A suppressed delegation delivery is stored hidden with its delegation_id; a client-submitted
    # hidden prompt can carry only a title preview.
    runtime_hidden = kind == "hidden" and bool(meta.get("delegation_id"))
    if kind not in _RUNTIME_NOTICE_KINDS and not runtime_hidden:
        return ""
    content = row.get("content")
    text = content.lstrip() if isinstance(content, str) else ""
    return next((label for prefix, label in _RUNTIME_NOTICE_LABELS if text.startswith(prefix)), "")


def last_visible_report(session_id: Optional[str], since: float = 0.0) -> str:
    """Newest visible final report from this goal's lifetime, including archived rows.

    Return prose verbatim for the judge to interpret, not a runtime-classified claim.
    Hidden rows, compression summaries and tool-call commentary are not visible reports.
    """
    db = _get_session_db()
    reader = getattr(db, "messages_by_role", None) if db is not None else None
    if not session_id or since <= 0 or db is None or reader is None:
        return ""
    try:
        for row in reader(session_id, "assistant", since=since, limit=500):
            content = row.get("content")
            if not isinstance(content, str) or not content.strip() or row.get("compressed_summary") or row.get("display_kind") == "hidden":
                continue
            if is_intentional_silence_response(content):
                continue
            # The role audit includes assistant tool-call rows. Only final prose
            # is a visible report; consult the original row, not a display summary.
            original = db.get_messages_around(session_id, row["id"], window=0).get("window") or []
            if original and not original[0].get("tool_calls"):
                return content
    except Exception as exc:
        logger.debug("goal last visible report: message read failed: %s", exc)
    return ""


def resolve_cited_evidence(session_id: Optional[str], response: str, since: float = 0.0) -> Dict[str, Any]:
    """Locate each identifier the response cites in tool results recorded since ``since``.

    Returns ``{"cited": [{needle, tool, excerpt, timestamp, message_id}], "unresolved": [...],
    "evidence_ids": [message ids]}``. Only tool-layer rows count, so the agent cannot cite its own prose into
    evidence. Excerpts are secret-redacted and bounded. Fail-safe: any error yields no citations."""
    empty: Dict[str, Any] = {"cited": [], "unresolved": [], "evidence_ids": []}
    needles = extract_citations(response)
    if not session_id or not needles:
        return empty
    db = _get_session_db()
    finder = getattr(db, "find_messages_containing", None) if db is not None else None
    call_finder = getattr(db, "find_tool_results_for_call", None) if db is not None else None
    if finder is None:
        return empty
    try:
        from agent.redact import redact_sensitive_text
    except Exception:  # pragma: no cover - redaction module is part of the runtime
        redact_sensitive_text = None
    cited: List[Dict[str, Any]] = []
    unresolved: List[str] = []
    # Excerpted ranges per result row: a citation inside an already-shown range points at it,
    # while a citation elsewhere in the same long result still gets its own excerpt.
    shown: Dict[Any, List[Tuple[int, int]]] = {}
    half = _CITATION_CONTEXT_CHARS // 2
    for needle in needles:
        matches: List[Tuple[Dict[str, Any], str, bool]] = []
        variants = _citation_variants(needle)
        if not variants:
            unresolved.append(needle)
            continue
        try:
            # A cited command resolves to what running it returned; anything else to where it appears.
            if call_finder is not None and len(needle) >= 8 and not needle.endswith(("…", "...")):
                matches += [(r, needle, True) for r in call_finder(session_id, needle, since=since, limit=4,
                                                                   **_EVIDENCE_EXCLUSION_KW)]
            for variant in variants:
                if len(matches) >= _CITATION_ROWS_PER_NEEDLE * 2:
                    break
                matches += [(r, variant, False) for r in finder(session_id, variant, role="tool", since=since,
                                                                limit=4, **_EVIDENCE_EXCLUSION_KW)]
        except Exception as exc:
            logger.debug("goal evidence: citation lookup failed: %s", exc)
            return empty
        matches = [m for m in matches if not _evidence_tool_excluded(str(m[0].get("tool_name") or ""))]
        if not matches:
            # Runtime-delivered results arrive as user-role notices, never agent prose.
            try:
                for variant in variants:
                    # A composed commit URL is only corroborated by a hash in a tool result.
                    if variant != needle and needle.startswith(("https://", "http://")) and \
                            re.search(r"/commit/[0-9a-fA-F]{7,40}$", needle):
                        continue
                    notices = [r for r in finder(session_id, variant, role="user", since=since, limit=4)
                               if _runtime_notice_label(r)]
                    if notices:
                        matches = [(dict(r, tool_name=_runtime_notice_label(r)), variant, False)
                                   for r in notices]
                        break
            except Exception as exc:
                logger.debug("goal evidence: notice lookup failed: %s", exc)
        if not matches:
            unresolved.append(needle)
            continue
        kept = 0
        for row, hit, via_call in matches:
            if kept >= _CITATION_ROWS_PER_NEEDLE or len(cited) >= _CITATION_MAX_EXCERPTS:
                break
            commit = re.search(r"/commit/([0-9a-fA-F]{7,40})$", needle)
            match_note = (f"(matched by commit hash {hit}; URL not verified) "
                          if commit and hit == commit.group(1) and hit != needle else "")
            content = str(row.get("content") or "")
            if via_call:
                span = (-1, -1)   # the call's own result tail: one excerpt per row
            else:
                at = max(0, content.find(hit))
                span = (at, at + len(hit))
            ranges = shown.setdefault(row.get("id"), [])
            covered = next((r for r in ranges if (span == (-1, -1) and r == span)
                            or (span != (-1, -1) and r[0] <= span[0] and span[1] <= r[1])), None)
            kept += 1
            if covered is not None:
                cited.append({"needle": needle, "tool": str(row.get("tool_name") or "tool"),
                              "excerpt": match_note + f"(inside the excerpt of result #{row.get('id')} above)",
                              "timestamp": float(row.get("timestamp") or 0.0), "message_id": row.get("id")})
                continue
            if via_call:
                ranges.append(span)
                excerpt = "ran " + _one_line(row.get("arguments"), _EVIDENCE_CALL_CHARS) + " → " + \
                    _tail(content.strip(), _CITATION_CONTEXT_CHARS).replace("\n", " ⏎ ")
            else:
                start, end = max(0, span[0] - half), min(len(content), span[1] + half)
                ranges.append((start, end))
                excerpt = ("…" if start else "") + content[start:end].replace("\n", " ⏎ ") + \
                    ("…" if end < len(content) else "")
            if redact_sensitive_text is not None:
                excerpt = redact_sensitive_text(excerpt, force=True)
            cited.append({"needle": needle, "tool": str(row.get("tool_name") or "tool"), "excerpt": match_note + excerpt,
                          "timestamp": float(row.get("timestamp") or 0.0), "message_id": row.get("id")})
    evidence_ids = sorted({str(c["message_id"]) for c in cited if c.get("message_id") is not None})
    return {"cited": cited, "unresolved": unresolved, "evidence_ids": evidence_ids}


def _render_cited_block(citations: Optional[Dict[str, Any]], now: Optional[float] = None) -> str:
    if not citations:
        return ""
    now = time.time() if now is None else now
    lines = []
    for item in citations.get("cited") or []:
        age = max(0, int(now - float(item.get("timestamp") or now)))
        lines.append(f"- `{item['needle']}` → {item['tool']} result #{item.get('message_id')} ({age}s ago): "
                     f"{item['excerpt']}")
    block = JUDGE_CITED_EVIDENCE_BLOCK_TEMPLATE.format(cited_lines="\n".join(lines)) if lines else ""
    unresolved = list(citations.get("unresolved") or [])[:_CITATION_MAX_UNRESOLVED_SHOWN]
    if unresolved:
        block += JUDGE_UNRESOLVED_CITATIONS_TEMPLATE.format(unresolved=", ".join(f"`{u}`" for u in unresolved))
    return block


def _call_goal_judge_llm(call_llm, system_prompt: str, user_prompt: str, timeout: Optional[float]) -> str:
    """Route through call_llm so auxiliary.goal_judge.* config (provider/model, extra_body,
    reasoning_effort, retries) all apply. Returns the raw reply text."""
    # See #35566.
    # Route through call_llm — same #35566 fix as the judge call above.
    resp = call_llm(
        task="goal_judge",
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0, max_tokens=_goal_judge_max_tokens(), timeout=timeout,
    )
    try:
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


def judge_goal(
    goal: str,
    last_response: str,
    *,
    timeout: Optional[float] = None,
    subgoals: Optional[list[str]] = None,
    background_processes: Optional[list[dict[str, Any]]] = None,
    contract: Optional[GoalContract] = None,
    active_delegations: int = 0,
    evidence: Optional[List[Dict[str, Any]]] = None,
    citations: Optional[Dict[str, Any]] = None,
    last_visible_report: str = "",
    user_messages_block: str = "",
    resume_override: Union[bool, List[str]] = False,
) -> Tuple[str, str, bool, Optional[Dict[str, Any]], bool]:
    """Ask the auxiliary model whether the goal is satisfied.

    ``evidence`` is the ledger from :func:`collect_goal_evidence`: tool results the runtime recorded,
    shown to the judge so verification gathered with tools counts without being pasted into prose.

    Returns ``(verdict, reason, parse_failed, wait_directive, transport_failed)``; verdict is done /
    blocked / continue / wait / skipped. ``parse_failed`` means unusable output; transport errors
    set ``transport_failed`` instead and fail-open to ``continue``.
    """
    if not goal.strip():
        return "skipped", "empty goal", False, None, False
    # A successful empty or tool-only turn is still judged from recorded evidence; skip the judge
    # only when there is nothing at all to evaluate (the evidence-free callers keep this path).
    has_record = bool(evidence) or bool(last_visible_report.strip()) or bool(
        citations and (citations.get("cited") or citations.get("unresolved")))
    if not last_response.strip() and not has_record:
        return "continue", "empty response (nothing to evaluate)", False, None, False
    if timeout is None:
        timeout = _goal_judge_timeout()   # the declared default is the config key, not the constant

    try:
        from agent.auxiliary_client import call_llm
        from agent.auxiliary_unavailable import AuxiliaryClientUnavailable
    except Exception as exc:
        logger.debug("goal judge: auxiliary client import failed: %s", exc)
        return "continue", "auxiliary client unavailable", False, None, False

    # Prompt priority: contract > subgoals > plain. With both, subgoals fold into the contract
    # block as extra criteria so the judge sees a single source of truth.
    clean_subgoals = contract_items("subgoals", subgoals or [])
    waived = resume_override if isinstance(resume_override, list) else []
    # Criteria are authoritative, unlike the bounded response preview. Silently
    # dropping later requirements makes the judge evaluate a different goal.
    common = dict(
        goal=goal,
        response=(_judge_response_window(last_response) if last_response.strip()
                  else JUDGE_EMPTY_RESPONSE_PLACEHOLDER),
        last_visible_report_block=(JUDGE_LAST_VISIBLE_REPORT_BLOCK_TEMPLATE.format(
            report=_judge_response_window(last_visible_report)) if last_visible_report else ""),
        evidence_block=_render_evidence_block(evidence),
        cited_block=_render_cited_block(citations),
        resume_override_block=(JUDGE_RESUME_OVERRIDE_RULE + "".join(
            f"- {_truncate(reason, 300)}\n" for reason in waived) + "\n" if resume_override else ""),
        user_messages_block=user_messages_block,
        background_block=_render_background_block(background_processes)
        + (JUDGE_DELEGATIONS_BLOCK_TEMPLATE.format(count=active_delegations) if active_delegations > 0 else ""),
        current_time=safe_strftime(datetime.now(tz=UTC).astimezone(), "%Y-%m-%d %H:%M:%S %Z"),
    )
    if contract is not None and not contract.is_empty():
        contract_block = contract.render_block()
        if clean_subgoals:
            contract_block = f"{contract_block}\n{_render_extra_criteria(clean_subgoals)}"
        prompt = JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE.format(contract_block=contract_block, **common)
    elif clean_subgoals:
        subgoals_block = "\n".join(f"- {i}. {text}" for i, text in enumerate(clean_subgoals, start=1))
        prompt = JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE.format(subgoals_block=subgoals_block, **common)
    else:
        prompt = JUDGE_USER_PROMPT_TEMPLATE.format(**common)

    try:
        raw = _call_goal_judge_llm(call_llm, JUDGE_SYSTEM_PROMPT, prompt, timeout)
    except AuxiliaryClientUnavailable as exc:
        # No client at all (e.g. a dead Nous refresh token): name the cause so the user is sent to
        # re-authenticate, not to context-length / model debugging (#42177). Still fails open.
        logger.info("goal judge: auxiliary client unavailable (%s) — falling through to continue", exc)
        return "continue", f"goal_judge auxiliary client unavailable: {exc}", False, None, True
    except Exception as exc:
        logger.info("goal judge: API call failed (%s) — falling through to continue", exc)
        return "continue", f"judge error: {type(exc).__name__}", False, None, True

    verdict, reason, parse_failed, wait_directive = _parse_judge_response(raw)
    logger.info("goal judge: verdict=%s reason=%s%s", verdict, _truncate(reason, 120),
                f" wait={wait_directive}" if wait_directive else "")
    return verdict, reason, parse_failed, wait_directive, False


def count_active_delegations(session_id: Optional[str]) -> int:
    """Live async delegation batches spawned by this session (fail-safe 0)."""
    if not session_id:
        return 0
    try:
        from tools.async_delegation import _LIVE_STATES, _session_records
        return len(_session_records(_LIVE_STATES, "", "", str(session_id)))
    except Exception:
        return 0


# `/goal <text>` kicks the loop by sending the goal as the next user turn. When that text IS what
# the user just said (a pasted handoff note, a plan the agent already has), re-sending it makes the
# agent spend a turn deciding it is a replay (11 API calls, 6 min, in one run) and duplicates ~2k
# tokens of context. The pointer is used only when the goal is substantially the WHOLE last
# message: a short goal that merely appears inside a longer one ("ship the API" after a message
# offering API or UI work) selects one option, and two different goals must not kick identically.
GOAL_ALREADY_SEEN_KICK = "[Goal set] Continue with the goal you were just given; there is no need to re-read it."
_GOAL_REPASTE_MIN_CHARS = 400
_GOAL_REPASTE_MIN_SHARE = 0.8


def goal_kick_prompt(goal: str, last_user_message: Any) -> str:
    """The goal text, or ``GOAL_ALREADY_SEEN_KICK`` when ``last_user_message`` is essentially that text."""
    content = last_user_message
    if isinstance(content, list):
        content = " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    goal_norm, last_norm = " ".join(str(goal or "").split()), " ".join(str(content or "").split())
    if (
        len(goal_norm) >= _GOAL_REPASTE_MIN_CHARS
        and goal_norm in last_norm
        and len(goal_norm) >= _GOAL_REPASTE_MIN_SHARE * len(last_norm)
    ):
        return GOAL_ALREADY_SEEN_KICK
    return goal


def last_user_message_content(history: Any) -> Any:
    """Content of the newest ``role == "user"`` message in an OpenAI-shaped history, else ``""``."""
    for msg in reversed(history or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return msg.get("content")
    return ""


def last_user_message_from_db(session_id: Optional[str]) -> Any:
    """Newest user message of ``session_id`` from the SessionDB (gateway/TUI surfaces have no live
    history object at slash-command time); ``""`` on any error."""
    if not session_id:
        return ""
    try:
        db = _get_session_db()
        if db is None:
            return ""
        rows = db.get_messages(str(session_id), limit=20, latest=True)
        return last_user_message_content(rows)
    except Exception:
        return ""


def gather_background_processes(task_id: Optional[str] = None, *, owner_task_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Fail-safe snapshot of RUNNING ``process_registry`` sessions for the judge; ``[]`` on any error
    so the loop degrades to its pre-wait-barrier behavior.

    ``owner_task_id`` restricts the snapshot to processes the goal's OWN session spawned. The registry's
    ``task_id`` is the container key, which collapses to one value for every agent in the process, so
    without this filter a fan-out parent's judge saw every subagent's pollers and parked the goal on a
    grandchild's ``proc_*`` session (one run: 7 of 7 root verdicts were WAIT on child-owned processes;
    parked 3 h 22 min at the end while nothing of its own was running)."""
    try:
        from tools.process_registry import process_registry

        sessions = process_registry.list_sessions(task_id=task_id) or []
    except Exception as exc:
        logger.debug("gather_background_processes failed: %s", exc)
        return []
    running = [s for s in sessions if isinstance(s, dict) and s.get("status") != "exited"]
    if owner_task_id:
        running = [s for s in running if str(s.get("owner_task_id") or s.get("task_id") or "") == str(owner_task_id)]
    return running


def draft_contract(objective: str, *, timeout: Optional[float] = None) -> Optional[GoalContract]:
    """Expand a plain-language objective into a completion contract via the ``goal_judge`` auxiliary
    task (a side LLM call, not a conversation turn). None when unavailable or unparseable."""
    objective = (objective or "").strip()
    if not objective:
        return None
    if timeout is None:
        # The declared default for this path is the config key, not the module constant — see
        # _goal_judge_timeout (#91022).
        # Same config-backed default as judge_goal (#91022).
        timeout = _goal_judge_timeout()

    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:
        logger.debug("goal draft: auxiliary client import failed: %s", exc)
        return None

    try:
        raw = _call_goal_judge_llm(call_llm, DRAFT_CONTRACT_SYSTEM_PROMPT, f"Objective:\n{_truncate(objective, 4000)}", timeout)
    except Exception as exc:
        logger.info("goal draft: API call failed (%s)", exc)
        return None

    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        logger.debug("goal draft: reply was not JSON: %r", _truncate(raw, 200))
        return None
    contract = GoalContract.from_dict(data)
    return None if contract.is_empty() else contract


# ── GoalManager — the orchestration surface CLI + gateway talk to ──────

# Runtime-injected user-role messages: never evidence of what the user said. Provenance
# (display_kind, compression flags) is the primary test; this list catches legacy rows written
# before the runtime typed every injection.
_SYNTHETIC_USER_PREFIXES = (
    "[Continuing toward", "[ASYNC DELEGATION", "[IMPORTANT:", "[System note", "[System:", "[CONTEXT COMPACTION",
    "[PRIOR CONTEXT", "[STILL IN PROGRESS", "[Cron delivery", "[Your active task list", "[Relay from",
    "[Goal set]", "[/loop wakeup",
)

_RELAY_HEADER_RE = re.compile(
    r"^\[relay from=hermes:(?P<profile>[^/\s\]]+)/(?P<session>[^\s\]]+) "
    r"receipt=[^\s\]]+(?: task=[^\s\]]+)?\]$"
)
_ANY_RELAY_HEADER_RE = re.compile(
    r"^\[relay from=[^\s\]]+ receipt=[^\s\]]+(?: task=[^\s\]]+)?\]$"
)
_USER_MESSAGES_MAX = 20
_USER_MESSAGES_MAX_CHARS = 8000


def _relay_sender(content: str) -> Optional[Tuple[str, str]]:
    """Return ``(profile, session_id)`` from a Hermes relay header on the first or last non-empty line."""
    if not isinstance(content, str):
        return None
    lines = [line.strip() for line in _unwrap_steer(content).splitlines() if line.strip()]
    for line in (lines[-1:] + lines[:1]) if lines else []:
        match = _RELAY_HEADER_RE.fullmatch(line)
        if match:
            return match.group("profile"), match.group("session")
    return None


def _relay_receipt_id(content: str) -> str:
    lines = [line.strip() for line in _unwrap_steer(str(content or "")).splitlines() if line.strip()]
    for line in (lines[-1:] + lines[:1]) if lines else []:
        match = re.search(r"\breceipt=([^\s\]]+)", line) if _RELAY_HEADER_RE.fullmatch(line) else None
        if match:
            return match.group(1)
    return ""


def _relay_ledger_receipts(receipt_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Newest receipt row per wanted id from relay's own ledger, read once.

    The header in a transcript is plain text any agent can write; the ledger is written by the relay
    transport at send time and records sender, target and the delivered transcript row."""
    wanted = {rid for rid in receipt_ids if rid}
    if not wanted:
        return {}
    state_dir = os.environ.get("RELAY_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".local/state/relay")
    path = os.path.join(state_dir, "receipts.jsonl")
    found: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                if '"id"' not in line or not any(rid in line for rid in wanted):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("id") in wanted:
                    found[row["id"]] = row
    except OSError:
        return {}
    return found


# Sessions whose user rows are written by agents or schedulers, never typed by a person.
_NON_HUMAN_SESSION_SOURCES = frozenset({"subagent", "cron", "tool", "kanban", "oneshot", "dryrun"})


def _authenticated_relay_sender(db: Any, relay: Dict[str, Any], goal_session_id: str,
                                profile: str, receipts: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The sender session of a relay row when relay's ledger proves this exact delivery, else None.

    Requires a typed-shaped row (no runtime display_kind, not a synthetic/continuation prompt), a
    Hermes header naming this profile, a ledger receipt with the same sender delivered to this goal
    session at this exact row, and a sender that is a human topic session."""
    content = str(relay.get("content") or "")
    if str(relay.get("display_kind") or "") not in _USER_TYPED_KINDS or relay.get("compressed_summary"):
        return None
    if content.lstrip().startswith(_SYNTHETIC_USER_PREFIXES):
        return None
    sender = _relay_sender(content)
    if sender is None or not profile or sender[0] != profile or sender[1] == goal_session_id:
        return None
    receipt = receipts.get(_relay_receipt_id(content))
    if not receipt or str(receipt.get("status") or "") not in ("delivered", "replied"):
        return None
    if receipt.get("sender") != f"hermes:{profile}/{sender[1]}":
        return None
    if receipt.get("target") != f"hermes:{profile}/{goal_session_id}":
        return None
    # The header is agent-writable text, so only a receipt bound to this exact transcript row proves
    # delivery; an unbound (legacy) receipt could be replayed under a forged header. Fail closed.
    row_id = receipt.get("target_user_row")
    if row_id is None or str(row_id) != str(relay.get("id")):
        return None
    try:
        info = db.get_session(sender[1]) or {}
    except Exception:
        return None
    if not info or str(info.get("source") or "") in _NON_HUMAN_SESSION_SOURCES:
        return None
    # Delegate children inherit the gateway source (e.g. telegram); the creation marker is the proof.
    # Continuations copy model_config verbatim, so a delegate child's continuation carries it too.
    raw_config = info.get("model_config")
    config: Any = raw_config
    if isinstance(raw_config, str) and raw_config:
        try:
            config = json.loads(raw_config)
        except ValueError:
            return None
    if isinstance(config, dict) and config.get("_delegate_from"):
        return None
    return sender[1]


def _unwrap_steer(content: str) -> str:
    """The steer payload without the OUT-OF-BAND markers, so its own edge lines can be inspected."""
    text = content.strip()
    try:
        from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN
    except Exception:  # pragma: no cover - prompt builder ships with the runtime
        return text
    if text.startswith(STEER_MARKER_OPEN):
        text = text[len(STEER_MARKER_OPEN):]
        if text.rstrip().endswith(STEER_MARKER_CLOSE):
            text = text.rstrip()[: -len(STEER_MARKER_CLOSE)]
    return text.strip()


def _is_agent_origin(content: str) -> bool:
    """Whether a complete relay header line (any sender kind) appears anywhere in the row.

    The gateway's edge-line rule is not enough here: the busy queue joins rapid follow-ups with a
    bare newline, so a relay and Brian's next message can share one row with the header mid-text,
    and nothing marks where the relay ends. Such a row is never authority, so fail closed."""
    for text in (str(content or ""), _unwrap_steer(str(content or ""))):
        for line in text.splitlines():
            if _ANY_RELAY_HEADER_RE.fullmatch(line.strip()):
                return True
    try:
        from gateway.response_filters import is_agent_origin_text
        return bool(is_agent_origin_text(content))
    except Exception:  # pragma: no cover - gateway ships with the runtime
        return False


def _active_profile_name() -> str:
    """The turn's profile as relay addresses name it: the bound session profile, else HERMES_HOME's."""
    try:
        from gateway.session_context import bound_session_env
        if profile := bound_session_env("HERMES_SESSION_PROFILE"):
            return str(profile)
    except Exception:
        pass
    try:
        from hermes_cli.profiles import get_active_profile_name
        return str(get_active_profile_name() or "")
    except Exception:
        return ""


def _user_message_rows(session_id: str, since: float, *, until: float = 0.0,
                       limit: int = 500) -> List[Dict[str, Any]]:
    """Newest-first typed user rows in ``[since, until)`` (``until`` 0 means now). Page raw rows
    until the typed bound is satisfied, so runtime rows cannot evict an approval from the window."""
    db = _get_session_db()
    reader = getattr(db, "messages_by_role", None) if db is not None else None
    if not session_id or reader is None:
        return []
    try:
        wanted = max(int(limit), 1)
        page_size = 500
        before_id = 0
        typed: List[Dict[str, Any]] = []
        while len(typed) < wanted:
            kwargs: Dict[str, Any] = {"since": since, "limit": page_size, "before_id": before_id,
                                    "current_only": True}
            if until > 0:
                kwargs["until"] = until
            page = reader(session_id, "user", **kwargs)
            typed.extend(row for row in page if _is_user_typed(row))
            if len(page) < page_size:
                break
            before_id = int(page[-1]["id"])
        return typed[:wanted]
    except Exception as exc:
        logger.debug("goal judge: user message read failed: %s", exc)
        return []


def _relay_rows(db: Any, session_id: str, since: float, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Keyset-page relay deliveries in the goal window, scanning all rows by default.

    Relay deliveries are sparse among user rows, so a raw-row cap can hide an older valid delivery
    behind hundreds of newer notifications. The default scans the complete bounded goal window.
    """
    wanted = None if limit is None else max(int(limit), 1)
    page_size = 500
    before_id = 0
    relays: List[Dict[str, Any]] = []
    while wanted is None or len(relays) < wanted:
        try:
            page = db.messages_by_role(session_id, "user", since=since, limit=page_size,
                                       before_id=before_id, current_only=True)
        except Exception as exc:
            logger.debug("goal judge: relay row read failed: %s", exc)
            break
        relays.extend(row for row in page if _relay_sender(str(row.get("content") or "")) is not None)
        if len(page) < page_size:
            break
        before_id = int(page[-1]["id"])
    return relays if wanted is None else relays[:wanted]


def _render_user_messages_block(session_id: Optional[str], since: float) -> str:
    """Render bounded authoritative user messages, including typed messages from relay senders."""
    if not session_id or since <= 0:
        return ""
    db = _get_session_db()
    if db is None:
        return ""
    entries: List[Tuple[float, int, str]] = []
    seen: set[Tuple[str, Any]] = set()
    current_rows = _user_message_rows(session_id, since)
    for row in current_rows:
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            key = (session_id, row.get("id"))
            if key not in seen:
                seen.add(key)
                entries.append((float(row.get("timestamp") or 0), int(row.get("id") or 0),
                                f"- Brian (this session):\n{content}"))

    active_profile = _active_profile_name()
    relay_rows = _relay_rows(db, session_id, since)
    receipts = _relay_ledger_receipts(
        _relay_receipt_id(str(r.get("content") or "")) for r in relay_rows
        if _relay_sender(str(r.get("content") or "")) is not None)
    for relay in relay_rows:
        sender_session = _authenticated_relay_sender(db, relay, session_id, active_profile, receipts)
        if sender_session is None:
            continue
        # Only what the user typed in the sender session before this delivery can be what it carries.
        relay_at = float(relay.get("timestamp") or 0)
        if relay_at <= 0:
            continue
        title = None
        try:
            title = db.get_session_title(sender_session)
        except Exception:
            pass
        label = title or sender_session
        for row in _user_message_rows(sender_session, since, until=relay_at):
            content = row.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            key = (sender_session, row.get("id"))
            if key in seen:
                continue
            seen.add(key)
            entries.append((float(row.get("timestamp") or 0), int(row.get("id") or 0),
                            f"- Brian (in session {label}):\n{content}"))
    if not entries:
        return ""
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    # Pack newest-first and stop at the first message that does not fit: skipping it could keep an
    # older approval while dropping a newer refusal. Never excerpt an authority message.
    kept: List[str] = []
    chars = 0
    for _timestamp, _id, text in entries[:_USER_MESSAGES_MAX]:
        if chars + len(text) > _USER_MESSAGES_MAX_CHARS:
            label = text.split("\n", 1)[0]
            kept.append(f"{label}\n[message of {len(text)} characters omitted as too long to judge; "
                        "it and every older message are not shown and grant no authority]")
            break
        kept.append(text)
        chars += len(text)
    kept.reverse()
    return JUDGE_USER_MESSAGES_BLOCK_TEMPLATE.format(user_message_lines="\n".join(kept)) if kept else ""


# Goal authority and recorded-user/evidence helpers live in a topical sibling.
# The imports keep the facade patch seam used by CLI, gateway, loops and tests.
_REVISION_EVIDENCE_MIN_CHARS = 12
from hermes_cli.goals_authority import (
    _AUTHORITY_PRESERVE_RE, _REPLACEMENT_ACTION_RE, _REPLACEMENT_BUDGET_TARGET_RE,
    _REPLACEMENT_GATE_TARGET_RE, _REPLACEMENT_NEGATION_RE, _REMOVAL_VERBS,
    _REVISION_QUOTE_MIN_CHARS, _REVISION_SOURCE_MAX_CHARS, _REPLY_QUOTE_RE,
    _NON_AUTHORITATIVE_QUOTE_RE, _NON_AUTHORITATIVE_SPANS_RE,
    _authority_unit_records, _authority_units, _bounded_authority_match,
    _bounded_evidence_match, _direct_authority_unit, _evidence_is_negated,
    _explicit_restriction_removals, _is_user_typed, _mask_non_authoritative_spans,
    _mask_replacement_objective_spans, _normalize_authority_text,
    _normalize_authority_value, _removal_authority_text, _removal_instruction_units,
    _replacement_action_is_authorized, _replacement_goal_is_negated, _requirement_diff,
    _typed_user_message_records_since, _typed_user_message_text, _unit_is_non_directive,
    user_messages_since,
)



# One notice per judge outage for the whole process, not one per goal: a provider outage
# hits every active goal at once. Require a later failed attempt; any success resets both flags.
_JUDGE_OUTAGE_LOCK = threading.Lock()
_JUDGE_OUTAGE_PENDING = _JUDGE_OUTAGE_ACTIVE = False
JUDGE_OUTAGE_NOTICE = "⚠ Goal judge unavailable; goals stay active and back off until it recovers."


def _judge_outage_notice(*, failed: bool) -> str:
    """Announce only consecutive failed attempts; ``failed=False`` ends the outage."""
    global _JUDGE_OUTAGE_PENDING, _JUDGE_OUTAGE_ACTIVE
    with _JUDGE_OUTAGE_LOCK:
        if not failed:
            _JUDGE_OUTAGE_PENDING = _JUDGE_OUTAGE_ACTIVE = False
            return ""
        if not _JUDGE_OUTAGE_PENDING:
            _JUDGE_OUTAGE_PENDING = True
            return ""
        if _JUDGE_OUTAGE_ACTIVE:
            return ""
        _JUDGE_OUTAGE_ACTIVE = True
        return JUDGE_OUTAGE_NOTICE


def _decision(status, should_continue: bool, prompt: Optional[str], verdict: str, reason: str, message: str) -> Dict[str, Any]:
    return {"status": status, "should_continue": should_continue, "continuation_prompt": prompt,
            "verdict": verdict, "reason": reason, "message": ContextNotice(message) if verdict == "done" else message}


_JUDGE_CONFIG_HINT = (
    "~/.hermes/config.yaml:\n  auxiliary:\n    goal_judge:\n      provider: {provider}\n      model: {model}\n"
    "Then /goal resume to continue."
)


class GoalPersistenceError(RuntimeError):
    """A durable goal change failed, so callers must not acknowledge or schedule it."""


class GoalRowCorrupt(GoalPersistenceError):
    """The stored goal row exists but cannot be parsed; only ``clear()`` may replace it."""


class _GoalMutationRefused(Exception):
    """A command mutation rejected the live row; carries the original error out of the txn."""

    def __init__(self, error: BaseException):
        super().__init__(str(error))
        self.error = error


def _live_goal(state: Optional[GoalState]) -> GoalState:
    if state is None or state.status not in {"active", "paused"}:
        raise RuntimeError("no active goal")
    return state


class GoalManager(GoalPeersMixin, GoalAutonomyMixin, GoalStatusMixin, GoalRevisionsMixin, GoalContractCommandsMixin):
    """Per-session goal state + continuation decisions.

    The CLI and gateway each hold one per live session. ``evaluate_after_turn`` calls the judge and
    returns the decision dict that drives the next turn; ``next_continuation_prompt`` is the
    canonical user-role message to feed back into ``run_conversation``.
    """

    def __init__(self, session_id: str, *, default_max_turns: int = DEFAULT_MAX_TURNS,
                 min_continuation_gap_seconds: Optional[float] = None, cursor=None):
        self._snapshot_lock = threading.RLock()
        self.session_id = session_id
        self.default_max_turns = normalize_goal_max_turns(default_max_turns)
        if min_continuation_gap_seconds is None:
            # Callers that own the gateway/config boundary pass the resolved
            # default explicitly. Keeping the manager's library default disabled
            # preserves direct CLI/TUI and test callers that intentionally drive
            # turns synchronously without a wall-clock scheduler.
            min_continuation_gap_seconds = 0
        self.min_continuation_gap_seconds = normalize_goal_continuation_gap(min_continuation_gap_seconds)
        # Session controls lend their write transaction; ordinary drivers keep independent writes.
        self._cursor = cursor
        # A row that exists but cannot be read must not break every user turn or the commands that
        # recover it. The manager holds no state (no continuation, judge, or revive) and remembers
        # why; writes still re-read the live row and refuse to overwrite it.
        self._unreadable: Optional[GoalPersistenceError] = None
        self._state: Optional[GoalState] = None
        self.refresh()

    # --- introspection ------------------------------------------------

    @property
    def state(self) -> Optional[GoalState]:
        return self._state

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_goal(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    @property
    def unreadable(self) -> bool:
        """True when a stored goal row exists but this manager could not load it."""
        return self._unreadable is not None

    def has_contract(self) -> bool:
        return self._state is not None and self._state.has_contract()

    # --- mutation -----------------------------------------------------

    def _save(self) -> Optional[GoalState]:
        snapshot = getattr(self, "_evaluation_snapshot", None)
        if snapshot is not None:
            from hermes_cli.goals_evaluation import assert_goal_snapshot
            db, expected = snapshot
            assert_goal_snapshot(self.session_id, expected, db)
            return self._state
        if self._cursor is None:
            save_goal(self.session_id, self._state)
        else:
            save_goal(self.session_id, self._state, cursor=self._cursor)
        return self._state

    def _mutate_current(self, mutate, *, discard_corrupt: bool = False):
        """Apply ``mutate(state)`` to the CURRENT durable goal row in one write transaction.

        Commands and the evaluator run on different managers (the gateway loads the command
        manager and dispatches it in separate executor hops), so the in-memory snapshot can be
        stale. Saving it blindly would undo a concurrent verdict: re-activating a budget pause or
        refunding consumed turns. Instead the row is re-read under ``BEGIN IMMEDIATE``, the edit is
        applied to that live state (lifecycle fields stay whatever is current), and the result is
        written with a fresh mutation token. The caller's manager is refreshed from the row, so
        continuation decisions after a command see current state. ``mutate`` receives ``None``
        when no row exists, must validate before mutating, and must not touch the database (the
        transaction may be replayed on lock contention). Inside an evaluator snapshot the edit is
        staged locally; the evaluator's own compare-and-set commit settles it. A row that cannot be
        parsed refuses every mutation except ``discard_corrupt``, which tombstones it unparsed.
        """
        if getattr(self, "_evaluation_snapshot", None) is not None or getattr(self, "_staging_internal", False):
            # Evaluator-internal transitions (judge waits, no-progress parks) act on the judged
            # snapshot; the evaluator's compare-and-set commit settles them.
            result = mutate(self._state)
            if self._state is not None:
                self._save()
            return result
        db = _get_session_db() if self.session_id else None
        if db is None or not hasattr(db, "_execute_write"):
            result = mutate(self._state)
            if self._state is not None:
                save_goal(self.session_id, self._state)   # warns on a dropped write
            return result
        key = _meta_key(self.session_id)
        seen: List[Optional[GoalState]] = []

        def _txn(conn):
            seen.clear()
            row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
            try:
                live = _parse_goal_row(self.session_id, row[0]) if row and row[0] else None
            except GoalRowCorrupt:
                if not discard_corrupt:
                    raise
                # A cleared tombstone keeps the instance fence monotonic for the next /goal set.
                tombstone = GoalState(goal="", status="cleared", created_at=time.time(),
                                      mutation_id=uuid.uuid4().hex)
                conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (tombstone.to_json(), key))
                return None, None
            # Compare normalized JSON so a no-op command never mints a token (which would
            # needlessly interrupt an in-flight evaluation).
            original = live.to_json() if live is not None else None
            if live is not None:
                _migrate_legacy_revision_requirements(live)
            seen.append(None if live is None else GoalState.from_json(live.to_json()))
            try:
                result = mutate(live)
                if live is not None:
                    validate_peer_completion(live)
            except Exception as exc:
                raise _GoalMutationRefused(exc) from exc   # rolls the transaction back
            if live is not None and live.to_json() != original:
                live.mutation_id = uuid.uuid4().hex
                conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (live.to_json(), key))
            return live, result

        if self._cursor is not None:
            try:
                live, result = _txn(self._cursor)
            except _GoalMutationRefused as refused:
                if seen:
                    self._adopt(seen[0])
                raise refused.error from None
            except GoalRowCorrupt:
                raise
            except Exception as exc:
                logger.warning("GoalManager: atomic goal mutation for %s not persisted: %s", self.session_id, exc)
                raise GoalPersistenceError("goal change was not saved; try again") from exc
            self._adopt(live)
            return result

        try:
            live, result = db._execute_write(_txn)
        except _GoalMutationRefused as refused:
            if seen:
                self._adopt(seen[0])
            raise refused.error from None
        except GoalRowCorrupt:
            raise
        except Exception as exc:
            logger.warning("GoalManager: atomic goal mutation for %s not persisted: %s", self.session_id, exc)
            raise GoalPersistenceError("goal change was not saved; try again") from exc
        self._adopt(live)
        return result

    def _adopt(self, live: Optional[GoalState]) -> None:
        """Refresh from the written row, keeping object identity for callers holding ``state``."""
        if live is None or self._state is None:
            self._state = live
        else:
            self._state.__dict__.update(live.__dict__)
            if not hasattr(live, "_legacy_migration_pending"):
                self._state.__dict__.pop("_legacy_migration_pending", None)

    def _require_goal(self) -> GoalState:
        if self._state is None or not self.has_goal():
            raise RuntimeError("no active goal")
        return self._state

    def _require_active(self) -> GoalState:
        if self._state is None or self._state.status != "active":
            raise RuntimeError("no active goal to park")
        return self._state

    def _pause_state(self, reason: str) -> None:
        self._state.clear_wait()
        self._state.status = "paused"
        self._state.paused_reason = reason
        self._state.paused_by = "system"
        self._save()

    def _pause_decision(self, paused_reason: str, verdict: str, reason: str, message: str) -> dict[str, Any]:
        if goal_autonomy_enabled():
            return self._autonomy_retry(paused_reason)
        self._pause_state(paused_reason)
        return _decision("paused", False, None, verdict, reason, message)

    def _next_instance_time(self) -> float:
        """A new goal instance's created_at, strictly after the one it supersedes.

        created_at is the continuation instance fence, so a coarse or frozen clock must not let a
        replacement or re-set reuse the previous instance's value.
        """
        prior = self._state.created_at if self._state is not None else getattr(self, "_cleared_instance", 0.0)
        # Another manager may have set a newer instance since this one loaded: include the stored
        # row (cleared rows stay stored) so two instances never share a created_at.
        try:
            stored = load_goal(self.session_id) if self._cursor is None else load_goal(
                self.session_id, cursor=self._cursor)
        except Exception:
            stored = None
        prior = max(float(prior or 0.0), float(getattr(stored, "created_at", 0.0) or 0.0))
        return max(time.time(), prior + 1e-6)

    @snapshot_control
    def set(self, goal: str, *, max_turns: Optional[int] = None, contract: Optional[GoalContract] = None) -> GoalState:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal text is empty")
        # A cached manager may predate a restored row or a failed read. Re-read before replacing
        # rather than interpreting its stale None as proof that the durable goal is absent.
        live = load_goal(self.session_id)
        if live is not None and live.pending_peers and live.status not in {"cleared", "done"}:
            raise ValueError("resolve pending peer work or explicitly clear the goal before setting another")
        if getattr(live, "_legacy_migration_pending", False):
            self._state = live
            raise GoalPersistenceError(
                "existing goal migration was not saved; refusing to overwrite it; try again"
            )
        if goal_autonomy_enabled() and live is not None and live.status in {"active", "paused"}:
            raise ValueError("Use revise or replace to preserve the existing goal contract")
        self._state = GoalState(
            goal=goal, status="active", turns_used=0, created_at=self._next_instance_time(), last_turn_at=0.0,
            max_turns=self.default_max_turns if max_turns is None else normalize_goal_max_turns(max_turns),
            contract=contract if contract is not None else GoalContract(),
            revisions=self._history_for_next_goal(live),
        )
        return self._save()

    @snapshot_control
    def set_contract(self, contract: GoalContract) -> Optional[GoalState]:
        """Attach or replace the completion contract on the active goal."""
        def _apply(state):
            if state is not None:
                state.contract = contract or GoalContract()
            return state
        return self._mutate_current(_apply)

    @snapshot_control
    def pause(self, reason: str = "user-paused", *, by: str = "user") -> Optional[GoalState]:
        def _apply(state):
            if state is None or state.status == "cleared":
                return None   # a concurrent /goal clear stays cleared
            state.status = "paused"
            state.paused_reason = reason
            state.paused_by = by
            state.clear_wait()   # a wait barrier is meaningless once paused
            state.consecutive_no_progress = 0
            state.backoff_level = 0
            return state
        return self._mutate_current(_apply)

    @snapshot_control
    def resume(self, *, reset_budget: bool = True, user_override: bool = True,
               by: str = "user") -> Optional[GoalState]:
        """Resume through a live-row mutation, optionally recording a user override.

        ``by="agent"`` lifts only the agent's own pause, never a user or system pause."""
        def _apply(state):
            if state is None or state.status == "cleared":
                return None
            if by == "agent" and not (state.status == "paused" and state.paused_by == "agent"):
                return None
            prior_block_reason = state.last_reason or state.paused_reason or "unknown blocker"
            record_override = user_override and (
                state.status == "paused" and state.last_verdict == "blocked"
                and (state.paused_reason or "").startswith((_BLOCKED_PAUSE_PREFIX, _LEGACY_BLOCKED_PAUSE_PREFIX))
            )
            if record_override:
                before = {"goal": state.goal, **state.contract.to_dict(), "subgoals": list(state.subgoals)}
                state.revisions.append({
                    "at": time.time(), "actor": "user", "user_command": "/goal resume",
                    "reason": f"Brian resumed over block: {prior_block_reason}",
                    "user_quote": "", "user_message": "", "before": before, "after": before,
                })
            self._resume_state(state, reset_budget=reset_budget)
            return state
        return self._mutate_current(_apply)

    @staticmethod
    def _resume_state(state: GoalState, *, reset_budget: bool) -> None:
        state.status = "active"
        state.paused_reason = None
        state.paused_by = ""
        state.consecutive_no_progress = 0
        state.backoff_level = 0
        state.clear_wait()
        if reset_budget:
            state.turns_used = 0

    @snapshot_control
    def resume_for_user_input(self) -> bool:
        """Reactivate a goal paused by the judge for required user input.

        Runs on every admitted user turn, so a goal that cannot be read or saved stays as it is and
        the turn proceeds; it never raises into the turn."""
        if self._unreadable is not None:
            return False

        def _apply(state):
            if state is None or state.status != "paused" or state.last_verdict != "blocked":
                return False
            if not (state.paused_reason or "").startswith((_BLOCKED_PAUSE_PREFIX, _LEGACY_BLOCKED_PAUSE_PREFIX)):
                return False
            self._resume_state(state, reset_budget=False)
            return True
        try:
            return self._mutate_current(_apply)
        except GoalPersistenceError as exc:
            logger.warning("GoalManager: blocked goal for %s not revived: %s", self.session_id, exc)
            return False

    @snapshot_control
    def clear(self) -> None:
        def _apply(state):
            if state is not None:
                state.status = "cleared"
        self._mutate_current(_apply, discard_corrupt=True)
        if self._state is not None:
            self._cleared_instance = self._state.created_at
        self._state = None
        self._unreadable = None

    # --- /goal wait barrier -------------------------------------------

    def _park(self, reason: str, **barrier) -> GoalState:
        self._require_active()

        def _apply(state):
            if state is None or state.status != "active":
                raise RuntimeError("no active goal to park")
            # Re-parking is not progress: keep the last notice key so an unchanged judge WAIT
            # remains silent, while clearing barrier fields before applying the new target.
            state.clear_wait(preserve_notice_key=True)
            for k, v in barrier.items():
                setattr(state, k, v)
            if goal_autonomy_enabled() and (state.waiting_on_pid or state.waiting_on_session):
                state.waiting_until = time.time() + self._autonomy_wait_seconds()
            if state.waiting_on_pid is not None:
                state.waiting_on_pid_live_at_park = _pid_alive(state.waiting_on_pid)
            if state.waiting_on_session is not None:
                state.waiting_on_session_live_at_park = _session_waiting(state.waiting_on_session)
            state.waiting_reason = (reason or "").strip() or None
            state.waiting_since = time.time()
            # A live barrier's first recheck is derived from waiting_since below rather than
            # persisted here.  The three goal surfaces can park the same state in separate
            # calls, so storing a wall-clock value at park time makes their state snapshots
            # differ even though the durable behavior is identical.
            return state
        return self._mutate_current(_apply)

    @snapshot_control
    def wait_on(self, pid: int, reason: str = "") -> GoalState:
        """Park the goal loop until a background PID exits (no turn burned, no judge call). For a
        process with a watch/notify trigger prefer ``wait_on_session``. Requires an active goal."""
        self._require_active()
        pid = int(pid)
        if pid <= 0:
            raise ValueError("pid must be a positive integer")
        if not _pid_alive(pid):
            raise ValueError("pid is not alive on this host")
        return self._park(reason, waiting_on_pid=pid)

    @snapshot_control
    def wait_on_session(self, session_id: str, reason: str = "") -> GoalState:
        """Park on a process_registry session's OWN trigger: exit OR ``watch_patterns`` match. The
        right barrier for a long-lived watcher/poller that signals mid-run and may never exit."""
        self._require_active()
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id must be a non-empty string")
        return self._park(reason, waiting_on_session=session_id)

    @snapshot_control
    def wait_for_seconds(self, seconds: int, reason: str = "", *, on_delegations: int = 0) -> GoalState:
        """Park until ``seconds`` from now (backoff/cooldown waits with no process to track). With
        ``on_delegations`` the wait is FOR those live delegation batches: it also lifts as soon as
        fewer are live (a batch result came back), so the loop re-judges with the result in hand
        instead of sleeping out a 20-minute timer (independent review: results arrived with 1,199 s
        left on the timer and nothing re-judged)."""
        self._require_active()
        seconds = int(seconds)
        if seconds <= 0:
            raise ValueError("seconds must be a positive integer")
        return self._park(
            reason, waiting_until=time.time() + seconds,
            waiting_on_delegations=max(0, int(on_delegations)), waiting_seconds=seconds,
        )

    @snapshot_control
    def stop_waiting(self) -> bool:
        """Clear any active wait barrier (pid / session / time). Returns True if one was cleared."""
        def _apply(s):
            if s is None or (s.waiting_on_pid is None and s.waiting_on_session is None
                             and not s.waiting_on_user_message and not s.waiting_until):
                return False
            s.clear_wait()
            return True
        return self._mutate_current(_apply)

    @snapshot_control
    def mark_automatic_continuation_dispatched(
        self, dispatched_at: Optional[float] = None, *,
        expected_instance: Optional[float] = None,
        expected_fingerprint: Optional[str] = None,
    ) -> bool:
        """Stamp an accepted idle-watcher continuation when its admission is accepted.

        The post-turn evaluator stamps continuations it decides to emit, but the lifted-barrier
        watcher dispatches before that evaluator runs. Recording the admission here makes that wake
        the automatic-chain anchor too. Expected identity fields prevent a stale wake from pacing a
        replacement goal, and a compressed successor is followed only when it carries the same goal
        instance."""
        stamp = time.time() if dispatched_at is None else float(dispatched_at)
        expected_created_at = None if expected_instance is None else float(expected_instance)

        def _apply(state):
            if state is None or state.status != "active":
                return False
            if expected_created_at is not None and state.created_at != expected_created_at:
                return False
            if expected_fingerprint is not None:
                from hermes_cli.session_controls import _definition_fingerprint
                if _definition_fingerprint("goal", state.to_json()) != expected_fingerprint:
                    return False
            state.last_automatic_continuation_at = max(
                float(state.last_automatic_continuation_at or 0.0), stamp,
            )
            return True

        marked = self._mutate_current(_apply)
        if marked or expected_created_at is None:
            return bool(marked)

        # Compression can archive the original row after admission but before this mutation. Follow
        # the validated successor and preserve the same instance fence instead of stamping a stale
        # parent or an unrelated replacement.
        from hermes_cli.session_controls import migrated_successor
        successor = migrated_successor("goal", self.session_id or "")
        moved = load_goal(successor) if successor else None
        if moved is None or moved.created_at != expected_created_at:
            return False
        self.session_id = successor
        return self.mark_automatic_continuation_dispatched(
            dispatched_at=stamp, expected_instance=expected_created_at,
            expected_fingerprint=expected_fingerprint,
        )

    @snapshot_control
    def clear_lifted_wait(self, waiting_since: float) -> bool:
        """Clear the barrier an idle surface just resumed, only if it is still that same wait
        (matched by ``waiting_since`` on the durable row). The resumed turn may already have finished
        and parked again; a blind ``stop_waiting`` would erase that newer barrier.

        Compression can migrate the goal between the barrier check and admission; admission then
        rebinds the continuation to the successor, so the wait it lifted lives there too."""
        cleared, current = clear_goal_wait_if_since(self.session_id, waiting_since)
        if not cleared and current is not None and current.status == "cleared":
            from hermes_cli.session_controls import migrated_successor

            successor = migrated_successor("goal", self.session_id or "", {"status": "cleared"})
            moved = load_goal(successor) if successor else None
            if moved is not None and moved.created_at == current.created_at:
                self.session_id = successor
                cleared, current = clear_goal_wait_if_since(successor, waiting_since)
        if current is not None:
            self._state = current
        return cleared

    def is_parked(self) -> bool:
        """True when an active goal carries a wait barrier, whether or not it still holds."""
        s = self._state
        return bool(s is not None and s.status == "active" and (
            s.waiting_on_pid is not None or s.waiting_on_session is not None or getattr(s, "waiting_on_user_message", False) or s.waiting_until))

    def automatic_continuation_floor_remaining(self) -> float:
        """Return the remaining automatic-chain floor for a possible idle-watcher wake."""
        state = self._state
        if state is None or state.status != "active":
            return 0.0
        previous = float(state.last_automatic_continuation_at or 0.0)
        if previous <= 0:
            return 0.0
        floor = max(float(self.min_continuation_gap_seconds), float(DEFAULT_MIN_CONTINUATION_GAP_SECONDS))
        return max(0.0, floor - (time.time() - previous))

    def _barrier_holds(self) -> bool:
        """Pure check: True iff a barrier is set AND not yet satisfied (never mutates state)."""
        s = self._state
        if s is None:
            return False
        if self._autonomy_timer_elapsed():
            return False
        if s.waiting_on_session is not None:
            still = _session_waiting(s.waiting_on_session)
        elif s.waiting_on_pid is not None:
            still = _pid_alive(s.waiting_on_pid)
        elif s.waiting_on_user_message:
            return True
        elif s.waiting_until:
            still = time.time() < s.waiting_until
            if still and s.waiting_on_delegations > 0:
                # Set because of live delegations: lift the moment one of them returned.
                live = count_active_delegations(self.session_id)
                if live < s.waiting_on_delegations:
                    still = False
        else:
            return False
        return still

    def _stage_live_barrier(self) -> tuple[bool, Optional[str], Optional[str]]:
        s = self._state
        if s is None or s.status != "active" or not s.waiting_since:
            return False, None, None
        if self._autonomy_timer_elapsed():
            s.clear_wait(preserve_notice_key=True)
            return False, None, None
        if s.waiting_on_session is not None:
            still = _session_waiting(s.waiting_on_session)
        elif s.waiting_on_pid is not None:
            still = _pid_alive(s.waiting_on_pid)
        else:
            return False, None, None
        if not still:
            s.clear_wait(preserve_notice_key=True)
            return False, None, None
        return True, None, None

    def is_waiting(self) -> bool:
        """True iff a barrier is set AND not yet satisfied; never mutates durable state."""
        s = self._state
        if s is None or not (s.waiting_on_pid is not None or s.waiting_on_session is not None or getattr(s, "waiting_on_user_message", False) or s.waiting_until):
            return False
        return self._barrier_holds()

    def lifted_barrier_prompt(self) -> Optional[str]:
        """Pure prompt check for a parked goal whose barrier has lifted.

        Live pid/session barriers are checked only for their actual target status; they
        have no hidden age ceiling or re-arm state.
        """
        if not self.is_parked():
            return None
        if self._barrier_holds():
            return None
        # A timed wait requested by a user or external event may be shorter than the automatic
        # continuation floor. Keep the expired timer quiet until that floor elapses; pid/session
        # lifts and delegation completions remain immediate event wakes.
        state = self._state
        if state is not None and state.waiting_until \
                and state.waiting_on_pid is None and state.waiting_on_session is None \
                and not state.waiting_on_user_message:
            if state.waiting_on_delegations:
                # A delegation completion is an event wake, but timer expiry while the same batch
                # remains live is still an automatic continuation and must obey the floor.
                delegation_count = count_active_delegations(self.session_id)
                if delegation_count >= state.waiting_on_delegations \
                        and self.automatic_continuation_floor_remaining() > 0:
                    return None
            elif self.automatic_continuation_floor_remaining() > 0:
                return None
        if state is not None and state.waiting_on_session \
                and state.waiting_on_session_live_at_park is not True \
                and self.automatic_continuation_floor_remaining() > 0:
            return None
        if state is not None and state.waiting_on_pid \
                and state.waiting_on_pid_live_at_park is not True \
                and self.automatic_continuation_floor_remaining() > 0:
            return None
        if self._state.waiting_on_session and _completion_notice_pending(self._state.waiting_on_session):
            return None
        prompt = self.next_continuation_prompt()
        if not prompt:
            return None
        note = _barrier_lift_note(self._state)
        # A barrier-lift note describes the target outcome directly; avoid adding the generic
        # release-recheck wording when the wake also carries a diagnostic note.
        if note:
            prompt = prompt.replace(_UNCHANGED_AUTONOMOUS_RECHECK_RULE, "")
        return f"{prompt}\n\n{note}" if note else prompt

    # --- the main entry point called after every turn -----------------

    def _wait_notice_key(self, state: GoalState) -> str:
        """Stable identity for the current wait barrier, excluding elapsed presentation text."""
        reason = state.waiting_reason or ""
        if state.waiting_on_session is not None:
            target = f"session:{state.waiting_on_session}"
        elif state.waiting_on_pid is not None:
            target = f"pid:{state.waiting_on_pid}"
        elif state.waiting_on_user_message:
            target = "user-message"
        elif state.waiting_on_delegations:
            target = "delegations"
        else:
            target = "timed"
        return f"{target}|reason:{reason}"

    @staticmethod
    def _waiting_target(state: GoalState) -> str:
        if state.waiting_on_session is not None:
            return f"session {state.waiting_on_session}"
        if state.waiting_on_pid is not None:
            return f"pid {state.waiting_on_pid}"
        if state.waiting_on_user_message:
            return "your next message"
        return f"{max(0, int(state.waiting_until - time.time()))}s remaining"

    def _wait_notice_decision(
        self, state: GoalState, *, verdict: str, message: str, notify: bool = True,
    ) -> Dict[str, Any]:
        key = self._wait_notice_key(state)
        if not notify:
            message = ""
        elif state.last_wait_notice_key == key:
            message = ""
        else:
            state.last_wait_notice_key = key
            self._save()
        return _decision("active", False, None, verdict, state.waiting_reason or self._waiting_target(state), message)

    def _waiting_decision(self, state: GoalState, *, notice: Optional[str] = None) -> Dict[str, Any]:
        tgt = self._waiting_target(state)
        reason = state.waiting_reason or tgt
        if notice is None:
            notice = getattr(self, "_last_live_barrier_notice", None)
            self._last_live_barrier_notice = None
        if notice:
            # Age notices are a separate delivery domain from the initial parked notice. They
            # must reach the user even when the parked barrier key was already announced.
            return _decision("active", False, None, "waiting", reason, notice)
        return self._wait_notice_decision(
            state, verdict="waiting",
            message=f"⏳ Goal parked — waiting on {tgt}: {reason}",
        )

    def _apply_wait_directive(self, *args, **kwargs):
        prior = getattr(self, "_staging_internal", False)
        self._staging_internal = True
        try:
            return self._apply_wait_directive_impl(*args, **kwargs)
        finally:
            self._staging_internal = prior

    def _apply_wait_directive_impl(
        self, wait_directive: Dict[str, Any], reason: str, *, active_delegations: int = 0,
        automatic: bool = False, min_seconds: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Judge said WAIT: set the barrier and park. The counted turn stands (the judge ran) but no
        continuation fires; the loop resumes once the barrier clears. ``None`` = the barrier is
        unobservable here, so the caller continues instead."""
        state = self._require_active()
        if goal_autonomy_enabled() and wait_directive.get("user_message"):
            wait_directive = {"seconds": self._autonomy_wait_seconds()}
        if wait_directive.get("session_id"):
            tgt = f"session {self.wait_on_session(str(wait_directive['session_id']), reason=reason).waiting_on_session}"
        elif wait_directive.get("pid"):
            pid = int(wait_directive["pid"])
            try:
                tgt = f"pid {self.wait_on(pid, reason=reason).waiting_on_pid}"
            except ValueError:
                # A remote or already-exited pid is a barrier this host can never observe lifting
                # (#110826): the judge sees the same pid next turn and would re-park forever.
                # Catching wait_on's own liveness check (rather than probing first) closes the
                # window where the pid exits between a pre-check and the park. Treat it as stale
                # state, not a fresh event, and use the same automatic floor as other stale waits.
                floor = max(float(self.min_continuation_gap_seconds), float(DEFAULT_MIN_CONTINUATION_GAP_SECONDS))
                previous = float(state.last_automatic_continuation_at or 0.0)
                remaining = floor - (time.time() - previous) if previous else floor
                seconds = max(1, math.ceil(remaining))
                logger.info("goal judge: wait_on_pid %s is not alive on this host; using %ss floor wait", pid, seconds)
                self.wait_for_seconds(seconds, reason=_MIN_CONTINUATION_GAP_REASON)
                tgt = f"pid {pid} (stale; {seconds}s floor)"
        elif wait_directive.get("until"):
            raw_until = wait_directive["until"]
            until = _parse_wait_until(raw_until) if isinstance(raw_until, str) else float(raw_until)
            if until is None or until <= time.time():
                return None
            until = min(until, time.time() + _MAX_TIMED_WAIT_S)
            self._park(reason, waiting_until=until, waiting_seconds=max(0, int(until - time.time())))
            tgt = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
        elif wait_directive.get("user_message"):
            self._park(reason, waiting_on_user_message=True)
            tgt = "your next message"
        else:
            requested = max(1, int(wait_directive["seconds"]))
            level = min(max(0, state.backoff_level), len(_NO_PROGRESS_BACKOFF_S) - 1)
            if automatic:
                requested = max(requested, _NO_PROGRESS_BACKOFF_S[level])
                state.backoff_level = min(level + 1, len(_NO_PROGRESS_BACKOFF_S) - 1)
            delegation_floor = 10 * 60 if active_delegations else 0
            seconds = min(max(requested, delegation_floor, min_seconds), _MAX_TIMED_WAIT_S)
            self.wait_for_seconds(seconds, reason=reason, on_delegations=active_delegations)
            tgt = f"{seconds}s"
        return self._wait_notice_decision(
            state, verdict="wait",
            message=f"⏳ Goal parked (judge) — waiting on {tgt}: {reason}",
        )

    def _no_progress_wait(self, *args, **kwargs):
        prior = getattr(self, "_staging_internal", False)
        self._staging_internal = True
        try:
            return self._no_progress_wait_impl(*args, **kwargs)
        finally:
            self._staging_internal = prior

    def _no_progress_wait_impl(
        self, state: GoalState, reason: str, *, active_delegations: int = 0, min_seconds: int = 0,
    ) -> Dict[str, Any]:
        """Park after an automatic no-progress turn as a quiet, durable backoff."""
        level = min(max(0, state.backoff_level), len(_NO_PROGRESS_BACKOFF_S) - 1)
        state.backoff_level = min(level + 1, len(_NO_PROGRESS_BACKOFF_S) - 1)
        seconds = max(_NO_PROGRESS_BACKOFF_S[level], min_seconds)
        self.wait_for_seconds(seconds, reason=reason, on_delegations=active_delegations)
        return self._wait_notice_decision(
            state, verdict="wait",
            message="",
        )

    def _delegation_no_progress_wait(self, *args, **kwargs):
        prior = getattr(self, "_staging_internal", False)
        self._staging_internal = True
        try:
            return self._delegation_no_progress_wait_impl(*args, **kwargs)
        finally:
            self._staging_internal = prior

    def _delegation_no_progress_wait_impl(
        self, state: GoalState, reason: str, active_delegations: int, *, min_seconds: int = 0,
    ) -> Dict[str, Any]:
        """Park immediately when live delegations leave an automatic turn with no action."""
        level = min(max(0, state.backoff_level), len(_NO_PROGRESS_BACKOFF_S) - 1)
        seconds = max(10 * 60, _NO_PROGRESS_BACKOFF_S[level], min_seconds)
        state.backoff_level = min(level + 1, len(_NO_PROGRESS_BACKOFF_S) - 1)
        self.wait_for_seconds(seconds, reason=reason, on_delegations=active_delegations)
        return self._wait_notice_decision(
            state, verdict="wait",
            message=f"⏳ Goal parked — waiting for {active_delegations} active delegation(s): {reason}",
        )

    def _budget_pause(self, state: GoalState, verdict: str, reason: str, note: str = "") -> dict[str, Any]:
        if goal_autonomy_enabled():
            return self._autonomy_budget_checkpoint(state, reason)
        return self._pause_decision(
            f"turn budget exhausted ({_goal_budget_label(state.turns_used, state.max_turns)})", verdict, reason,
            f"⏸ Goal paused — {_goal_budget_label(state.turns_used, state.max_turns)} turns used{note}. "
            "Use /goal resume to keep going, or /goal clear to stop.",
        )

    def evaluate_after_turn(
        self, last_response: str, *, user_initiated: bool = True,
        external_event: bool = False,
        background_processes: Optional[List[Dict[str, Any]]] = None,
        active_delegations: int = 0,
        evidence_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Evaluate an isolated snapshot and atomically commit against its durable state.

        ``external_event`` marks a watched-process/delegation notification. These
        events are allowed to pierce a timed continuation gap because they carry
        new evidence; ordinary continuation-only turns are throttled.
        """
        from hermes_cli.goals_evaluation import evaluate_goal_snapshot
        return evaluate_goal_snapshot(
            self, last_response, user_initiated=user_initiated, external_event=external_event,
            background_processes=background_processes, active_delegations=active_delegations,
            evidence_session_id=evidence_session_id,
        )

    def _evaluate_after_turn(
        self, last_response: str, *, user_initiated: bool = True,
        external_event: bool = False,
        background_processes: Optional[List[Dict[str, Any]]] = None,
        active_delegations: int = 0,
        evidence_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run gates + judge and update state. Return a decision dict (``status``, ``should_continue``,
        ``continuation_prompt``, ``verdict``, ``reason``, ``message``). Both real user prompts and our
        own continuations increment ``turns_used`` — both consume model budget.

        ``evidence_session_id`` names the transcript whose tool results feed the judge's evidence
        ledger when it differs from the goal's key (the TUI keys goals by session key)."""
        state = self._state
        if not self.is_active():
            return _decision(state.status if state else None, False, None, "inactive", "no active goal", "")

        # A real break in silence resets the no-progress ladder. Discover the current
        # turn's evidence before any wait or gate return so an actionable result can reset it
        # even when this evaluator does not reach the judge.
        silent = is_intentional_silence_response(last_response)
        silent_automatic = not user_initiated and silent
        previous_turn_at = state.last_turn_at
        evidence = collect_goal_evidence(evidence_session_id or self.session_id, since=state.created_at, max_entries=None)
        turn_evidence = _evidence_for_turn(evidence, previous_turn_at)
        evidence_scan_truncated_for_turn = _evidence_scan_truncated_for_turn(evidence, previous_turn_at)
        new_eligible_non_read_only = (
            evidence_scan_truncated_for_turn or not _evidence_only_read_only_status(turn_evidence)
        )
        if state.waiting_on_pid is not None or state.waiting_on_session is not None or getattr(state, "waiting_on_user_message", False):
            holds, age_notice, pause_reason = self._stage_live_barrier()
            # A real user message or delivered external event is fresh evidence: it pierces
            # every wait kind and re-judges now. Synthetic continuation wakes do not.
            if user_initiated or external_event:
                state.clear_wait(preserve_notice_key=True)
                holds, age_notice, pause_reason = False, None, None
            elif getattr(state, "waiting_on_user_message", False):
                return self._waiting_decision(state)
            if pause_reason:
                return _decision("paused", False, None, "blocked", pause_reason,
                                 f"⏸ Goal paused — blocker: {pause_reason}. Use /goal resume after checking the target.")
            if holds:
                return self._waiting_decision(state, notice=age_notice)
        elif state.waiting_until:
            if self._barrier_holds():
                # A real user message or delivered external event is fresh evidence and re-judges
                # a waiting goal immediately. Synthetic continuation wakes do not pierce it.
                if user_initiated or external_event:
                    state.clear_wait(preserve_notice_key=True)
                else:
                    return self._waiting_decision(state)
            else:
                state.clear_wait(preserve_notice_key=True)

        state.turns_used += 1
        state.last_turn_at = time.time()
        if self._autonomy_budget_exhausted(state):
            return self._autonomy_budget_checkpoint(state, "turn pacing checkpoint reached")
        gap_floor = max(float(self.min_continuation_gap_seconds), float(DEFAULT_MIN_CONTINUATION_GAP_SECONDS))

        # Gates run BEFORE the judge: a failing gate is deterministic evidence the goal is not done,
        # so the judge is skipped and the gate's output drives the next turn (same turn budget).
        gate_decision = self._check_gates()
        if gate_decision is not None:
            if gate_decision.get("should_continue") and state.max_turns > 0 and state.turns_used >= state.max_turns:
                return self._budget_pause(state, "gate_failed", gate_decision.get("reason", ""), note=" (a quality gate is still failing)")
            if gate_decision.get("should_continue"):
                if not user_initiated and not external_event:
                    previous = float(state.last_automatic_continuation_at or 0.0)
                    remaining = gap_floor - (time.time() - previous) if previous else 0.0
                    if remaining > 0:
                        self.wait_for_seconds(max(1, math.ceil(remaining)), reason=_MIN_CONTINUATION_GAP_REASON)
                        return self._wait_notice_decision(state, verdict="waiting", message="", notify=False)
                state.last_automatic_continuation_at = time.time()
                self._save()
            return gate_decision

        # Keep the judge-facing ledger bounded even though progress classification needs the
        # complete current-turn slice. A bounded scan that omitted rows fails closed below.
        evidence = evidence[-_EVIDENCE_MAX_ENTRIES:]
        # Gates that just passed are deterministic evidence too, but their timestamp is a
        # presentation detail and must not affect no-progress identity.
        now = time.time()
        evidence += [
            {"tool": "quality gate", "call": f"$ {g.command}", "timestamp": now,
             "output": f"exit {g.last_exit_code} (passed)\n{_tail((g.last_output_tail or '').strip(), _EVIDENCE_OUTPUT_CHARS)}".strip()}
            for g in state.gates
        ]
        visible_report = (last_visible_report(evidence_session_id or self.session_id, since=state.created_at)
                          if silent_automatic else "")
        citations = resolve_cited_evidence(evidence_session_id or self.session_id, visible_report or last_response,
                                           since=state.created_at)
        progress_fingerprint = _goal_progress_fingerprint(turn_evidence)
        new_progress = bool(progress_fingerprint and progress_fingerprint != state.last_progress_fingerprint)
        # A bare silence marker is an explicit quiet repeat from a gateway-authored continuation.
        # It still reaches the judge: recorded evidence, not visible prose, is authoritative for completion.
        verdict, reason, parse_failed, wait_directive, transport_failed = judge_goal(
            state.goal, last_response, subgoals=state.subgoals or None, background_processes=background_processes,
            contract=state.contract if state.has_contract() else None, active_delegations=active_delegations,
            evidence=evidence or None, citations=citations,
            last_visible_report=visible_report,
            resume_override=state.resume_override_reasons(),
            user_messages_block=_render_user_messages_block(self.session_id, state.created_at),
        )
        # Track attempts before early returns so successes reset the outage; parse failures stay silent.
        outage_notice = "" if parse_failed else _judge_outage_notice(failed=bool(transport_failed))

        verdict, reason, wait_directive = autonomous_verdict(state, verdict, reason, wait_directive)
        state.last_verdict = verdict
        state.last_reason = reason
        if verdict == "continue" and not silent_automatic:
            state.last_wait_notice_key = None
        if progress_fingerprint:
            state.last_progress_fingerprint = progress_fingerprint
        if user_initiated or external_event:
            state.consecutive_no_progress = 0
            state.backoff_level = 0
        # An automatic turn makes progress only with new, actionable tool evidence the judge could
        # assess. Read-only status checks, a repeated evidence fingerprint, or a judge that could not
        # answer all count as no progress, so the turn backs off on the shared ladder.
        automatic_read_only = not user_initiated and not external_event and (
            not new_eligible_non_read_only
            or not (new_progress or evidence_scan_truncated_for_turn)
            or bool(transport_failed) or bool(parse_failed)
        )
        if automatic_read_only:
            # WAIT and CONTINUE share one durable no-progress streak. Changing status output,
            # judge wording, and a repeated parked turn are not actionable progress.
            state.consecutive_no_progress += 1
        else:
            state.consecutive_no_progress = 0
            state.backoff_level = 0
        # Judge failure counters are diagnostics only; neither kind pauses a goal.
        state.consecutive_parse_failures = state.consecutive_parse_failures + 1 if parse_failed else 0
        state.consecutive_transport_failures = state.consecutive_transport_failures + 1 if transport_failed else 0

        # Automatic no-progress waits already provide the sole pacing ladder. Fresh evidence
        # bypasses it by resetting backoff_level above; the hard floor still applies to the
        # resulting continuation.
        if verdict == "wait" and wait_directive:
            parked = self._apply_wait_directive(
                wait_directive, reason, active_delegations=active_delegations,
                automatic=not user_initiated and not external_event,
                min_seconds=(gap_floor if not user_initiated and not external_event else 0),
            )
            if parked is not None:
                return parked

        # BLOCKED is NOT done: pause for missing user input, an external prerequisite, or
        # an impossible goal. A recoverable dependency must never be called unachievable.
        if verdict == "blocked":
            from hermes_cli.goals_evaluation import blocked_goal_decision
            return blocked_goal_decision(self, reason)

        if verdict == "done":
            return self._finish_judged_goal(reason)

        def _with_outage(decision: Dict[str, Any]) -> Dict[str, Any]:
            if outage_notice and decision.get("status") == "active":
                decision["message"] = outage_notice
            return decision

        # Every automatic no-progress CONTINUE parks on the shared quiet ladder immediately. The
        # per-goal floor below also covers fresh-but-routine automatic evidence; visible silence
        # remains eligible for evidence-based completion on a silent recheck.
        if verdict == "continue" and automatic_read_only:
            return _with_outage(self._no_progress_wait(
                state, reason, active_delegations=active_delegations, min_seconds=gap_floor))

        # Fresh evidence does not make a goal exempt from the per-goal automatic-chain floor.
        # User messages and trusted external events remain immediate wake sources, but the
        # continuation they produce becomes the next automatic-chain anchor.
        if verdict == "continue" and not user_initiated and not external_event:
            previous = float(state.last_automatic_continuation_at or 0.0)
            remaining = gap_floor - (time.time() - previous) if previous else 0.0
            if remaining > 0:
                self.wait_for_seconds(max(1, math.ceil(remaining)), reason=_MIN_CONTINUATION_GAP_REASON)
                return _with_outage(self._wait_notice_decision(
                    state, verdict="waiting", message="", notify=False))

        if state.max_turns > 0 and state.turns_used >= state.max_turns:
            return self._budget_pause(state, "continue", reason)

        # Live delegation results are an explicit external wake source. If an automatic turn produced no
        # actionable evidence, use the same quiet ladder as every other no-progress turn.
        if verdict == "continue" and not user_initiated and active_delegations > 0 and (
            not new_progress or _evidence_only_read_only_status(turn_evidence)
        ):
            return _with_outage(self._delegation_no_progress_wait(
                state, reason, active_delegations, min_seconds=gap_floor))

        reason_text = (reason or "").strip()
        # Reuse S1's durable notice identity so continuation and parked-state notices
        # remain coordinated across internal turns and gateway restarts.
        notice_key = f"continuation|reason:{reason_text}"
        if silent_automatic or state.last_continuation_notice_key == notice_key:
            notice = ""
        else:
            state.last_continuation_notice_key = notice_key
            notice = f"↻ Continuing toward goal ({_goal_budget_label(state.turns_used, state.max_turns)}): {reason}"
        if outage_notice:
            notice = outage_notice
        elif transport_failed or parse_failed:
            notice = ""
        state.last_automatic_continuation_at = time.time()
        self._save()
        return _decision(
            "active", True, self.next_continuation_prompt(), "continue", reason,
            notice,
        )

    def next_continuation_prompt(self) -> Optional[str]:
        s = self._state
        if not s or s.status != "active":
            return None
        return self._current_continuation_prompt(s)

    @staticmethod
    def _current_continuation_prompt(s: "GoalState") -> str:
        if goal_autonomy_enabled():
            return AUTONOMY_CONTINUATION_TEMPLATE.format(
                goal=s.goal, contract_block=s.contract.render_block(), subgoals_block=s.render_subgoals_block())
        # Contract first (it carries the verification surface); subgoals fold in as extra criteria.
        if s.has_contract():
            contract_block = s.contract.render_block()
            if s.subgoals:
                contract_block = f"{contract_block}\n{_render_extra_criteria(s.subgoals)}"
            return CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE.format(goal=s.goal, contract_block=contract_block)
        if s.subgoals:
            return CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE.format(goal=s.goal, subgoals_block=s.render_subgoals_block())
        return CONTINUATION_PROMPT_TEMPLATE.format(goal=s.goal)

    def render_contract(self) -> str:
        """Public helper for the /goal show + /goal draft slash commands."""
        if self._state is None:
            return "(no active goal)"
        return self._state.contract.render_block() if self._state.has_contract() else (
            "(no completion contract — set one with /goal draft <objective> or inline field: value lines)")


# ── Kanban worker goal loop ───────────────────────────────────────────

# Fed to a kanban goal-mode worker that hasn't completed/blocked its task yet: short, and points it
# back at the lifecycle contract (it already has the full task body).
KANBAN_GOAL_CONTINUATION_PREFIX = "[Continuing toward this kanban task — judge says it is not done yet]\nReason:"
KANBAN_GOAL_CONTINUATION_TEMPLATE = (
    f"{KANBAN_GOAL_CONTINUATION_PREFIX} {{reason}}\n\n"
    "Take the next concrete step toward completing the task. When the work "
    "is genuinely finished, call kanban_complete with a summary. If it is a "
    "code change that needs same-card review before counting as done, call "
    "kanban_request_review with a summary instead. If you are blocked and "
    "need human input, call kanban_block with a reason. Do not stop without "
    "calling one of them."
)

# Judge says done but the worker never made a terminal board call
# (kanban_complete/kanban_request_review/kanban_block): one explicit nudge.
KANBAN_GOAL_FINALIZE_TEMPLATE = (
    "[The work looks complete, but the task is still open]\n"
    "Reason: {reason}\n\n"
    "If the task is genuinely done, call kanban_complete now with a short "
    "summary of what you did. If it is a code change awaiting same-card review, "
    "call kanban_request_review with that summary instead. If something still "
    "blocks completion, call kanban_block with the reason instead."
)


# Worker-driven terminal task statuses → loop outcome. The card's own acceptance criteria are the
# goal; the worker already has the full task body, so these outcomes stop the loop cleanly.
_KANBAN_TERMINAL_STATUSES = {
    "done": ("completed_by_worker", "worker completed the task", "task {task_id} completed by worker after {turns} turn(s)"),
    "blocked": ("blocked_by_worker", "worker blocked the task", "task {task_id} blocked by worker after {turns} turn(s)"),
    # kanban_request_review is a legitimate terminator: implementation done, awaiting a reviewer.
    "review": ("review_requested_by_worker", "worker requested review", "task {task_id} handed off for review by worker after {turns} turn(s)"),
    "changes_requested": ("changes_requested_by_reviewer", "reviewer requested changes", "reviewer returned task {task_id} for changes after {turns} turn(s)"),
}


def run_kanban_goal_loop(
    *,
    task_id: str,
    goal_text: str,
    run_turn,
    task_status_fn,
    block_fn,
    max_turns: int = DEFAULT_MAX_TURNS,
    first_response: str = "",
    log=None,
) -> dict[str, Any]:
    """Drive a kanban worker through a Ralph-style goal loop.

    Each iteration: stop if the worker already terminated the task (``kanban_complete`` /
    ``kanban_block`` / review hand-off); otherwise judge the latest response against ``goal_text``
    (the card's title + body) and feed a continuation or finalize nudge. A WAIT verdict is treated
    as CONTINUE (workers finish via kanban tools, not by parking).
    """

    def _log(msg: str) -> None:
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    def _block(message: str) -> None:
        try:
            block_fn(message)
        except Exception as exc:
            _log(f"kanban goal loop: block_fn failed ({exc})")

    def _result(outcome: str, reason: str) -> dict[str, Any]:
        return {"outcome": outcome, "turns_used": turns_used, "reason": reason}

    max_turns = int(max_turns or DEFAULT_MAX_TURNS)
    if max_turns < 1:
        max_turns = DEFAULT_MAX_TURNS

    last_response = first_response or ""
    turns_used = 1   # the first turn already consumed one unit of budget
    nudged_to_finalize = False

    while True:
        try:
            status = task_status_fn()
        except Exception as exc:
            _log(f"kanban goal loop: status check failed ({exc}); stopping")
            return _result("stopped", "status check failed")

        terminal = _KANBAN_TERMINAL_STATUSES.get(status)
        if terminal is not None:
            outcome, reason, log_fmt = terminal
            _log("kanban goal loop: " + log_fmt.format(task_id=task_id, turns=turns_used))
            return _result(outcome, reason)
        if status not in ("running", "ready"):
            # Reclaimed / archived / unexpected — let the dispatcher own it.
            _log(f"kanban goal loop: task {task_id} status={status!r}; stopping")
            return _result("stopped", f"status={status}")

        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task_id}")
        try:
            verdict, reason, _parse_failed, _wait, _transport_failed = judge_goal(goal_text, last_response)
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
        if _transport_failed:
            _log(f"kanban goal loop: judge transport failed on turn {turns_used}; stopping")
            return _result("stopped", "judge transport failure")
        if verdict == "wait":
            verdict = "continue"
        _log(f"kanban goal loop: turn {turns_used}/{max_turns} verdict={verdict} reason={_truncate(reason, 120)}")

        if verdict == "blocked":
            # Unachievable is NOT done: block the card with the judge's reason now instead of
            # re-poking an impossible goal, and never let it land in done.
            # The judge ruled the goal cannot be satisfied at all — this is NOT done (#100954).
            _log(f"kanban goal loop: task {task_id} judged unachievable; blocking")
            _block(f"Goal-mode judge ruled the goal unachievable: {reason}")
            return _result("blocked_unachievable", f"judge verdict blocked: {reason}")

        if verdict == "done":
            if nudged_to_finalize:
                # Already asked once to call kanban_complete — block for review rather than spin.
                _log(f"kanban goal loop: task {task_id} judged done but worker won't finalize; blocking")
                _block(
                    f"Goal-mode worker's output looked complete but it never "
                    f"called kanban_complete after a finalize nudge ({reason})."
                )
                return _result("blocked_budget", "judged done, never finalized")
            prompt = KANBAN_GOAL_FINALIZE_TEMPLATE.format(reason=_truncate(reason, 400))
            nudged_to_finalize = True
        else:
            prompt = KANBAN_GOAL_CONTINUATION_TEMPLATE.format(reason=_truncate(reason, 400))

        # Budget check BEFORE spending another turn.
        if turns_used >= max_turns:
            _log(f"kanban goal loop: task {task_id} exhausted {turns_used}/{max_turns} turns; blocking")
            _block(
                f"Goal-mode worker exhausted its turn budget "
                f"({turns_used}/{max_turns}) without completing the task. "
                f"Last judge verdict: {_truncate(reason, 300)}"
            )
            return _result("blocked_budget", "turn budget exhausted")

        try:
            result = run_turn(prompt)
            if isinstance(result, dict):
                last_response = result.get("response", "") or ""
                failed = bool(result.get("failed", False))
                failure_reason = result.get("failure_reason") or "unknown"
            else:
                # backward compatibility: assume it's a string
                last_response = result or ""
                failed = False
                failure_reason = None
        except Exception as exc:
            _log(f"kanban goal loop: run_turn failed ({exc}); stopping")
            return _result("stopped", f"run_turn error: {type(exc).__name__}")
        if failed:
            _log(f"kanban goal loop: worker failed on turn {turns_used} (reason={failure_reason}); stopping")
            return _result("stopped", f"worker failed: {failure_reason}")
        turns_used += 1


__all__ = [
    "GoalState", "GoalContract", "GoalGate", "GoalManager", "parse_contract", "draft_contract", "run_gate",
    "CONTINUATION_PROMPT_TEMPLATE", "CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE",
    "CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE", "JUDGE_USER_PROMPT_TEMPLATE",
    "JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE", "JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE",
    "DRAFT_CONTRACT_SYSTEM_PROMPT", "KANBAN_GOAL_CONTINUATION_TEMPLATE", "KANBAN_GOAL_FINALIZE_TEMPLATE",
    "DEFAULT_MAX_TURNS", "load_goal", "save_goal", "clear_goal", "migrate_goal_to_session", "judge_goal",
    "run_kanban_goal_loop", "normalize_goal_max_turns",
]
