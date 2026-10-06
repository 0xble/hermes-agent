"""Persistent session goals — the Ralph loop for Hermes.

A goal is a free-form objective that stays active across turns; after each turn an auxiliary-model
judge decides whether it is satisfied. The continuation prompt is a normal user message appended via
``run_conversation`` (no system-prompt mutation or toolset swap — prompt caching stays intact). Judge
failures are fail-OPEN (``continue``); the turn budget is the backstop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_cli._subprocess_compat import noninteractive_git_env
from hermes_time import safe_strftime

logger = logging.getLogger(__name__)


# ── Constants & defaults ──────────────────────────────────────────────

DEFAULT_MAX_TURNS = 20


def normalize_goal_max_turns(value: Any, default: int = DEFAULT_MAX_TURNS) -> int:
    """Normalize a goal budget; zero is the explicit unlimited sentinel."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed >= 0 else int(default)


def _goal_budget_label(turns_used: int, max_turns: int) -> str:
    return f"{turns_used}/∞" if max_turns == 0 else f"{turns_used}/{max_turns}"


DEFAULT_JUDGE_TIMEOUT = 30.0
# Judge output budget. Reasoning models burn hidden-reasoning tokens before the visible one-line
# JSON verdict; 200 (the original) reliably truncated it and tripped the auto-pause. 4096 covers
# every model live-tested; override via auxiliary.goal_judge.max_tokens.
DEFAULT_JUDGE_MAX_TOKENS = 4096
# Cap how much of the last response we send to the judge.
_JUDGE_RESPONSE_SNIPPET_CHARS = 4000
# A closeout usually ends with its evidence, so the judge also sees the response's tail.
_JUDGE_RESPONSE_TAIL_CHARS = 3000
# Consecutive judge *parse* failures (empty / non-JSON) before the loop auto-pauses and points at
# the goal_judge config. API/transport errors do NOT count — those are tracked separately below.
# Guards against small models that cannot follow the strict JSON contract burning the whole budget.
DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES = 3
# Consecutive transport failures (401, timeout, DNS) before auto-pause: a broken API key returns
# 401 every call and must not spend every turn on an unreachable judge.
DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES = 5
# Consecutive CONTINUE verdicts the judge marks ``disputed`` (the agent asserts the goal is done,
# the judge disagrees) before the loop pauses for the user. Re-poking an agent that believes it is
# finished only produces restated claims, so a persistent disagreement needs a human decision.
DEFAULT_MAX_CONSECUTIVE_DISPUTES = 3
# Evidence ledger: recent tool results recorded while the goal was active, shown to the judge so
# evidence gathered with tools counts without the agent pasting it into its prose reply.
_EVIDENCE_MAX_ENTRIES = 8
_EVIDENCE_SCAN_ROWS = 120
_EVIDENCE_OUTPUT_CHARS = 800
_EVIDENCE_CALL_CHARS = 240
# Bookkeeping and retrieval tools prove nothing about the goal's outcome.
_EVIDENCE_EXCLUDED_TOOLS = frozenset({
    "skill_view", "skills_list", "skill_manage", "tool_search", "tool_describe", "memory",
    "session_search", "todo", "todo_list", "clarify", "goal_set",
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
# Result ids remembered across one dispute streak.
_DISPUTE_SEEN_MAX = 200


def _evidence_id_order(value: str) -> Tuple[int, str]:
    return (int(value), value) if value.isdigit() else (0, value)
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
# ``paused_reason`` prefix of the dispute stall-breaker pause (agent says done, judge disagrees).
_DISPUTED_PAUSE_PREFIX = "judge disputed completion: "

# Quality gates: deterministic shell commands that must pass before the judge may declare DONE. A
# failed gate short-circuits the judge — its output IS the continuation prompt, so the agent works
# on concrete evidence instead of a vibe check.
DEFAULT_GATE_TIMEOUT_SECONDS = 300
DEFAULT_GATE_MAX_RETRIES = 3
# Longest a pid/session wait barrier may hold the loop before judging resumes. Timed barriers
# (``waiting_until``) carry their own deadline and are exempt.
_MAX_BARRIER_WAIT_S = 30 * 60
# Bounded tail of a failed gate's combined stdout/stderr fed back to the agent.
_GATE_OUTPUT_TAIL_CHARS = 3000


CONTINUATION_PROMPT_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Continue working toward this goal. Take the next concrete step. "
    "If you believe the goal is complete, state so explicitly, cite the proof "
    "(exact identifiers or output lines from tool results, in backticks), and stop. "
    "If you are blocked and need input from the user, say so clearly and stop."
)

# Appended to every continuation prompt once the goal has been revised. The runtime cannot tell
# whether a quoted user message really authorizes a change (the judge decides that after the
# fact), so the working agent keeps seeing each replaced requirement as binding: a prohibition the
# agent dropped on its own must still stop it before an irreversible action, not only at judging.
CONTINUATION_REVISIONS_TEMPLATE = (
    "\n\nThis goal has been revised. Each earlier requirement listed below still "
    "binds you unless the user message cited for that revision plainly instructs "
    "that specific change. When in doubt, honor the earlier requirement.\n"
    "{revision_lines}"
)

# With a completion contract: the block tells the agent what "done" means, how to prove it, what
# not to break, scope, and when to stop — so it targets the verification surface.
CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Completion contract:\n"
    "{contract_block}\n\n"
    "Continue working toward the outcome above. Take the next concrete step. "
    "Stay within the stated boundaries and do not violate the constraints. "
    "Your method may change as you learn; the end state may not. If a criterion "
    "has become obsolete or wrong, say so and revise the goal (goal_set "
    "action=revise when available, quoting the user's words when they changed "
    "scope) rather than working around it. Before claiming the goal is done, audit each "
    "Verification item against current state and end with an Evidence section "
    "that quotes exact identifiers or output lines from tool results in "
    "backticks (commit SHAs, run ids, URLs, `N passed` lines), so the runtime "
    "can locate them. If you hit the stated stop condition or are otherwise "
    "blocked and need user input, say so clearly and stop."
)

# With /subgoal criteria: surfaced verbatim to the agent and to the judge.
CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Additional criteria the user added mid-loop:\n"
    "{subgoals_block}\n\n"
    "Continue working toward the goal AND all additional criteria. Take "
    "the next concrete step. If you believe the goal and every "
    "additional criterion are complete, state so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly "
    "and stop."
)

# Fed back when a quality gate fails: bounded output is the evidence to repair against (no judge).
CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE = (
    "[Continuing toward your standing goal — a quality gate failed]\n"
    "Goal: {goal}\n\n"
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
    "DONE requires the deliverable to actually exist. If the response only "
    "explains why the goal cannot be reached, the verdict is BLOCKED, not "
    "DONE.\n\n"
    "BLOCKED — the goal cannot be satisfied as stated:\n"
    "- The response explains the goal is genuinely unachievable (impossible, "
    "out of scope, no valid path to the deliverable), or refuses to "
    "fabricate a deliverable that cannot exist, OR\n"
    "- Progress needs user input or an external prerequisite to proceed, "
    "and no authorized investigation or independent work remains right now. "
    "This is a resolvable blocker, NOT proof the whole goal is unachievable.\n"
    "Before choosing BLOCKED, prefer CONTINUE if the agent can investigate, "
    "adapt its method, or do independent authorized work. Return BLOCKED "
    "with the precise missing input or prerequisite in the reason; BLOCKED "
    "pauses the goal rather than completing it.\n"
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
    "nothing else dispatchable — return ``wait_for_seconds`` between 600 and "
    "1800. Their results wake the agent on their own; re-poking it now only "
    "produces a status recap.\n"
    "Picking WAIT parks the loop without burning a turn; it resumes "
    "automatically when the pid exits or the time elapses. Do NOT pick WAIT "
    "just because work remains — only when re-poking now would be pure "
    "busy-work because the agent can't progress until the async thing "
    "finishes.\n\n"
    "CONTINUE — not done, and there is a concrete next step the agent can "
    "take right now. This is the default when in doubt. When you return "
    "CONTINUE although the response asserts the goal is already complete, "
    "add \"disputed\": true and, in the reason, name the single criterion "
    "that lacks evidence and the concrete check that would prove it. Before "
    "disputing, look through the cited evidence and recorded tool results: "
    "evidence found there counts even if the response only summarizes it.\n\n"
    "Judge the end state, not the route. The agent may change its method, "
    "order, tools or plan as it learns; hold it to the outcome.\n\n"
    "Reply ONLY with a single JSON object on one line. Shapes:\n"
    '{"verdict": "done", "reason": "<one sentence>"}\n'
    '{"verdict": "blocked", "reason": "<one sentence>"}\n'
    '{"verdict": "continue", "reason": "<one sentence>"}\n'
    '{"verdict": "continue", "disputed": true, "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_session": "<id>", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_pid": <int>, "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_for_seconds": <int>, "reason": "<one sentence>"}\n'
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

# Judge prompt block for the evidence ledger (empty when nothing was recorded, so prompts without
# evidence stay byte-identical).
JUDGE_EVIDENCE_BLOCK_TEMPLATE = (
    "Tool results recorded by the runtime while this goal was active (oldest "
    "first; authoritative, not written by the agent). Treat them as concrete "
    "evidence: the response does not need to repeat them. Weigh their age, "
    "since a later change can make an earlier result stale:\n{evidence_lines}\n\n"
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
    "return CONTINUE with disputed and name the contradiction.\n\n"
)

# Judge prompt block for the goal's revision history (empty without revisions).
JUDGE_REVISIONS_BLOCK_TEMPLATE = (
    "Revision history (the goal and criteria above are the CURRENT version). "
    "A revision may clarify or restructure, but only the user can lower the "
    "bar. For each earlier requirement a revision dropped or weakened: it is "
    "superseded only when the cited user message plainly instructs that "
    "specific change; otherwise, including every revision with no user "
    "authority, hold the agent to the earlier requirement.\n{revision_lines}\n\n"
)

JUDGE_USER_PROMPT_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "{revisions_block}"
    "Agent's most recent response:\n{response}\n\n"
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
    "{revisions_block}"
    "Agent's most recent response:\n{response}\n\n"
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
    "{revisions_block}"
    "Agent's most recent response:\n{response}\n\n"
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
    "- If the response explains the work is genuinely unachievable or hits "
    "the stated Stop condition and needs user input, the goal is NOT done — "
    "return BLOCKED with the reason describing the block.\n"
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

    def to_dict(self) -> Dict[str, str]:
        return {f: getattr(self, f) for f in _CONTRACT_FIELDS}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GoalContract":
        if not isinstance(data, dict):
            return cls()
        return cls(**{f: str(data.get(f) or "").strip() for f in _CONTRACT_FIELDS})

    def render_block(self) -> str:
        """Non-empty fields as a labelled block; empty contract → empty string."""
        return "\n".join(f"- {_CONTRACT_LABELS[f]}: {getattr(self, f).strip()}" for f in _CONTRACT_FIELDS if getattr(self, f).strip())


def parse_contract(text: str) -> Tuple[str, GoalContract]:
    """Split user-typed goal text into a headline + contract from inline ``field: value`` lines.

    A headline without an explicit ``outcome:`` IS the outcome — it is not duplicated into the
    contract block (the goal text already carries it), so outcome stays empty in that case.
    """
    if not text:
        return "", GoalContract()
    headline_parts: List[str] = []
    fields: Dict[str, List[str]] = {f: [] for f in _CONTRACT_FIELDS}
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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GoalGate":
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


def _gate_workspace() -> Tuple[Optional[str], Optional[str]]:
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


def run_gate(gate: GoalGate, *, cwd: Optional[str] = None) -> Tuple[bool, int, str]:
    """Run one gate through the shell. Returns ``(passed, exit_code, output_tail)``; a timeout kills
    the process and counts as exit code -1."""
    try:
        # utf-8/replace: operator-configured output is arbitrary bytes; strict codepage decoding of
        # one unmappable byte (emoji/CJK on a non-UTF-8 Windows console) kills the reader thread and
        # the tail the agent needs arrives empty.
        proc = subprocess.run(
            gate.command, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(1, int(gate.timeout_seconds)), cwd=cwd or None,
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
    consecutive_parse_failures: int = 0       # judge-output parse failures in a row
    # Tracked separately from parse failures: a broken API key returns 401 every call and must
    # auto-pause instead of burning the budget on an unreachable judge.
    consecutive_transport_failures: int = 0   # judge API/transport errors in a row
    # CONTINUE verdicts in a row where the judge disputed the agent's completion claim.
    consecutive_disputes: int = 0
    # User-added criteria (/subgoal). Both the judge and continuation prompts include them.
    subgoals: List[str] = field(default_factory=list)
    # Wait barrier (judge ``wait`` verdict or ``/goal wait``): parks the loop instead of re-poking the
    # agent into busy-work. pid → until exit; session → until that process_registry session's OWN
    # trigger fires (exit OR watch_patterns match — preferred for watchers that signal mid-run);
    # until → wall-clock deadline. While ANY is active evaluate_after_turn returns
    # should_continue=False without burning a turn; cleared lazily when satisfied or by unwait/pause/
    # resume/clear. Defaults empty so old state_meta rows load unchanged.
    waiting_on_pid: Optional[int] = None
    waiting_on_session: Optional[str] = None
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
    # Versioned revisions of the goal/contract/subgoals: {at, actor, reason, user_quote, before, after}.
    # Shown to the judge and continuation so superseded wording stops binding. Old rows load as [].
    revisions: List[Dict[str, Any]] = field(default_factory=list)
    # Comma-joined ids of recorded results cited during the current dispute streak; a dispute that
    # cites none beyond these counts toward the stall breaker.
    last_dispute_evidence: str = ""
    # Stable identity of the last parked state announced to the user. This is durable so repeated
    # internal wakes and gateway restarts do not replay an unchanged wait notice.
    last_wait_notice_key: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "GoalState":
        data = json.loads(raw)
        raw_subgoals = data.get("subgoals") or []
        ints = {k: int(data.get(k) or 0) for k in (
            "turns_used", "consecutive_parse_failures", "consecutive_transport_failures", "consecutive_disputes",
            "waiting_on_delegations", "waiting_seconds")}
        floats = {k: float(data.get(k) or 0.0) for k in ("created_at", "last_turn_at", "waiting_until", "waiting_since")}
        return cls(
            goal=data.get("goal", ""),
            mutation_id=str(data.get("mutation_id") or ""),
            status=data.get("status", "active"),
            max_turns=normalize_goal_max_turns(data.get("max_turns", DEFAULT_MAX_TURNS)),
            last_verdict=data.get("last_verdict"),
            last_reason=data.get("last_reason"),
            paused_reason=data.get("paused_reason"),
            subgoals=[str(s).strip() for s in raw_subgoals if str(s).strip()] if isinstance(raw_subgoals, list) else [],
            waiting_on_pid=(int(data["waiting_on_pid"]) if data.get("waiting_on_pid") else None),
            waiting_on_session=(str(data["waiting_on_session"]) if data.get("waiting_on_session") else None),
            waiting_reason=data.get("waiting_reason"),
            contract=GoalContract.from_dict(data.get("contract")),
            gates=[
                GoalGate.from_dict(g) for g in (data.get("gates") or [])
                if isinstance(g, dict) and str(g.get("command") or "").strip()
            ],
            revisions=[r for r in (data.get("revisions") or []) if isinstance(r, dict)]
            if isinstance(data.get("revisions"), list) else [],
            last_dispute_evidence=str(data.get("last_dispute_evidence") or ""),
            last_wait_notice_key=(str(data["last_wait_notice_key"]) if data.get("last_wait_notice_key") else None),
            **ints, **floats,
        )

    def has_contract(self) -> bool:
        return self.contract is not None and not self.contract.is_empty()

    def render_subgoals_block(self) -> str:
        """Numbered ``- N. text`` block; empty when there are no subgoals."""
        return "\n".join(f"- {i}. {text}" for i, text in enumerate(self.subgoals, start=1))

    def render_revisions_block(self) -> str:
        """Every revision with every requirement it replaced, in full; empty without revisions.

        Nothing is windowed or truncated: a replaced requirement stays binding unless a user message
        instructs the change, so dropping it from the prompt would silently lower the bar."""
        lines = []
        for i, rev in enumerate(self.revisions, start=1):
            quote = str(rev.get("user_quote") or "").strip()
            source = str(rev.get("user_message") or "").strip()
            if quote:
                authority = f'cites the user: "{quote}"' + (f" (full message: \"{source}\")" if source else "")
            else:
                authority = "agent, no user authority"
            before, after = rev.get("before") or {}, rev.get("after") or {}
            changed = [k for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]
            lines.append(f"- v{i + 1} ({rev.get('actor') or 'agent'}, {authority}): "
                         f"{_truncate(str(rev.get('reason') or ''), 300)} — changed: {', '.join(changed) or 'nothing'}")
            for key in changed:
                if key in ("goal", "outcome", "verification", "constraints", "boundaries", "stop_when"):
                    lines.append(f"    earlier {key}: {str(before.get(key) or '(empty)')}")
                elif key == "subgoals":
                    dropped = [s for s in (before.get(key) or []) if s not in (after.get(key) or [])]
                    if dropped:
                        lines.append("    dropped criteria: " + "; ".join(str(s) for s in dropped))
        return "\n".join(lines)

    def clear_wait(self, *, preserve_notice_key: bool = False) -> None:
        self.waiting_on_pid = None
        self.waiting_on_session = None
        self.waiting_until = 0.0
        self.waiting_on_delegations = 0
        self.waiting_seconds = 0
        self.waiting_reason = None
        self.waiting_since = 0.0
        if not preserve_notice_key:
            self.last_wait_notice_key = None


# ── Persistence (SessionDB state_meta) ────────────────────────────────

def _meta_key(session_id: str) -> str:
    return f"goal:{session_id}"


_DB_CACHE: Dict[str, Any] = {}
_DB_BOOTSTRAP_LOCK = threading.Lock()
_DB_BOOTSTRAP_INFLIGHT: Dict[str, threading.Event] = {}

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


def load_goal(session_id: str) -> Optional[GoalState]:
    """Load the goal for a session, or None if none exists."""
    if not session_id:
        return None
    db = _get_session_db()
    if db is None:
        return None
    try:
        raw = db.get_meta(_meta_key(session_id))
    except Exception as exc:
        logger.debug("GoalManager: get_meta failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return GoalState.from_json(raw)
    except Exception as exc:
        logger.warning("GoalManager: could not parse stored goal for %s: %s", session_id, exc)
        return None


def save_goal(session_id: str, state: GoalState) -> None:
    """Persist a goal to SessionDB. No-op if DB unavailable."""
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        _warn_dropped_write("GoalManager", "goal", session_id)
        return
    try:
        state.mutation_id = uuid.uuid4().hex
        db.set_meta(_meta_key(session_id), state.to_json())
    except Exception as exc:
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
        has_wait = state.waiting_on_pid is not None or state.waiting_on_session is not None or state.waiting_until
        if state.status != "active" or not has_wait or state.waiting_since != waiting_since:
            return False, state
        state.clear_wait()
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


def _session_waiting(session_id: str) -> bool:
    """True while the process_registry session is running and its trigger hasn't fired. Fail-safe:
    any import/registry error yields False so a stale barrier can never wedge the loop."""
    if not session_id:
        return False
    try:
        from tools.process_registry import process_registry

        return bool(process_registry.is_session_waiting(session_id))
    except Exception:
        return False


def _process_outcome(session_id: str) -> Optional[Dict[str, Any]]:
    """Terminal facts for a process_registry session: the live registry first, then the durable
    receipt under ``logs/process-results``. None when neither knows it (outcome unknown)."""
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
            return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}background process {sid} is no longer tracked, most likely because "
                    "the gateway restarted. Its outcome is unknown; verify the real state before relying on it "
                    "and do not assume it succeeded.]")
        if outcome.get("running"):
            return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}background process {sid} is still running after "
                    f"{_MAX_BARRIER_WAIT_S // 60} minutes. Check its progress before waiting again.]")
        if outcome.get("completion_reason") == "killed":
            cause = ("a gateway restart or shutdown" if outcome.get("termination_source") == "kill_all"
                     else "an explicit kill")
            return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}background process {sid} was killed by {cause} before it finished "
                    f"(exit {outcome.get('exit_code')}). Its result is incomplete; check it with the process "
                    "tool and verify the real state before deciding whether to restart that work.]")
        return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}background process {sid} finished "
                f"({outcome.get('completion_reason') or 'exited'}, exit {outcome.get('exit_code')}). "
                "Read its output with the process tool before continuing.]")
    if state.waiting_on_pid:
        if _pid_alive(state.waiting_on_pid):
            return (f"{GOAL_WAIT_LIFTED_NOTE_OPEN}pid {state.waiting_on_pid} is still running after "
                    f"{_MAX_BARRIER_WAIT_S // 60} minutes. Check its progress before waiting again.]")
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
        if not raw or '"active"' not in raw:
            continue
        try:
            state = GoalState.from_json(raw)
        except Exception:
            continue
        if state.status == "active" and (
                state.waiting_on_pid is not None or state.waiting_on_session is not None or state.waiting_until):
            out.append((key[len("goal:"):], state))
    return out


def store_has_parked_goal(db: Any) -> bool:
    """True when *db* holds an active goal with a wait barrier. Propagates read errors so an idle
    gate can tell "empty" from "unavailable" (callers fail open)."""
    for _key, raw in db.list_meta_prefix("goal:"):
        if raw and '"active"' in raw:
            try:
                state = GoalState.from_json(raw)
            except Exception:
                return True
            if state.status == "active" and (
                    state.waiting_on_pid is not None or state.waiting_on_session is not None or state.waiting_until):
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


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
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


def _parse_judge_response(raw: str) -> Tuple[str, str, bool, Optional[Dict[str, Any]]]:
    """Parse the judge's reply, fail-open. Returns ``(verdict, reason, parse_failed, wait_directive)``.

    For a ``continue`` verdict the directive is ``{"disputed": True}`` when the judge flags that the
    agent claimed completion it could not verify, else None.

    ``parse_failed`` flags non-JSON output so callers can auto-pause after N in a row.
    ``wait_directive`` is ``{"session_id"}`` / ``{"pid"}`` / ``{"seconds"}`` for a ``wait``
    verdict; a wait with no target is downgraded to ``continue``. Accepts ``{"verdict": ...}`` and
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
    if verdict == "continue" and _truthy(data.get("disputed")):
        # The directive slot carries the dispute flag for CONTINUE (it is only read as a wait
        # target when verdict == "wait"), keeping the judge's 5-tuple contract unchanged.
        return verdict, reason, False, {"disputed": True}
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
    seconds = _first_int("wait_for_seconds", "seconds", "wait_seconds")
    if seconds is not None:
        return "wait", reason, False, {"seconds": seconds}
    return "continue", f"{reason} (wait verdict had no target — continuing)", False, None


def _render_background_block(background_processes: Optional[List[Dict[str, Any]]]) -> str:
    """Render RUNNING ``process_registry.list_sessions()`` entries for the judge prompt. Empty string
    when nothing is running, so the prompt stays byte-identical to the no-background case."""
    lines: List[str] = []
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


def collect_goal_evidence(session_id: Optional[str], since: float = 0.0, *,
                          max_entries: int = _EVIDENCE_MAX_ENTRIES) -> List[Dict[str, Any]]:
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
        rows = db.get_messages(session_id, limit=_EVIDENCE_SCAN_ROWS, latest=True)
    except Exception as exc:
        logger.debug("goal evidence: message read failed: %s", exc)
        return []
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
    return entries[-max_entries:]


def _render_evidence_block(evidence: Optional[List[Dict[str, Any]]], now: Optional[float] = None) -> str:
    """Judge prompt block for the evidence ledger; empty when there is none."""
    lines: List[str] = []
    now = time.time() if now is None else now
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        age = max(0, int(now - float(item.get("timestamp") or now)))
        head = f"- {item.get('tool') or 'tool'} ({age}s ago)"
        if item.get("call"):
            head += f": {item['call']}"
        output = str(item.get("output") or "").strip() or "(no output)"
        lines.append(f"{head}\n  output: {output}")
    if not lines:
        return ""
    return JUDGE_EVIDENCE_BLOCK_TEMPLATE.format(evidence_lines="\n".join(lines))


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
    subgoals: Optional[List[str]] = None,
    background_processes: Optional[List[Dict[str, Any]]] = None,
    contract: Optional[GoalContract] = None,
    active_delegations: int = 0,
    evidence: Optional[List[Dict[str, Any]]] = None,
    citations: Optional[Dict[str, Any]] = None,
    revisions_block: str = "",
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
    if not last_response.strip():
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
    clean_subgoals = [s.strip() for s in (subgoals or []) if s and s.strip()]
    # Criteria are authoritative, unlike the bounded response preview. Silently
    # dropping later requirements makes the judge evaluate a different goal.
    common = dict(
        goal=goal,
        response=_judge_response_window(last_response),
        evidence_block=_render_evidence_block(evidence),
        cited_block=_render_cited_block(citations),
        revisions_block=(JUDGE_REVISIONS_BLOCK_TEMPLATE.format(revision_lines=revisions_block)
                         if revisions_block.strip() else ""),
        background_block=_render_background_block(background_processes)
        + (JUDGE_DELEGATIONS_BLOCK_TEMPLATE.format(count=active_delegations) if active_delegations > 0 else ""),
        current_time=safe_strftime(datetime.now(tz=timezone.utc).astimezone(), "%Y-%m-%d %H:%M:%S %Z"),
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


def gather_background_processes(task_id: Optional[str] = None, *, owner_task_id: Optional[str] = None) -> List[Dict[str, Any]]:
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
    "[Goal set]",
)


def _is_user_typed(row: Dict[str, Any]) -> bool:
    """Whether a user-role row is input the person typed, judged by provenance first."""
    content = row.get("content")
    if not isinstance(content, str) or row.get("compressed_summary"):
        return False
    if str(row.get("display_kind") or "") not in _USER_TYPED_KINDS:
        return False
    if content.lstrip().startswith(_SYNTHETIC_USER_PREFIXES):
        return False
    try:
        from agent.context_compressor import ContextCompressor
        if ContextCompressor._is_context_summary_content(content):
            return False
    except Exception:  # pragma: no cover - compressor is part of the runtime
        pass
    return True
_REPLY_QUOTE_RE = re.compile(r'^\[Replying to: ".*?"\]\n\s*', re.DOTALL)


_REVISION_QUOTE_MIN_CHARS = 12
# Longest user message a revision may cite. The judge decides authority from the complete message,
# so a longer one is refused rather than excerpted: an excerpt can drop the context that negates it.
_REVISION_SOURCE_MAX_CHARS = 4000


def user_messages_since(session_id: Optional[str], since: float = 0.0, limit: int = 500) -> List[str]:
    """Text the user actually wrote since ``since``: synthetic runtime prompts are dropped and a
    reply's quoted header (which repeats the assistant's words) is stripped. Fail-safe: ``[]``."""
    db = _get_session_db()
    reader = getattr(db, "messages_by_role", None) if db is not None else None
    if not session_id or reader is None:
        return []
    try:
        rows = reader(session_id, "user", since=since, limit=limit)
    except Exception as exc:
        logger.debug("goal revise: user message read failed: %s", exc)
        return []
    return [_REPLY_QUOTE_RE.sub("", row["content"], count=1) for row in rows if _is_user_typed(row)]


def _decision(status, should_continue: bool, prompt: Optional[str], verdict: str, reason: str, message: str) -> Dict[str, Any]:
    return {"status": status, "should_continue": should_continue, "continuation_prompt": prompt,
            "verdict": verdict, "reason": reason, "message": message}


_JUDGE_CONFIG_HINT = (
    "~/.hermes/config.yaml:\n  auxiliary:\n    goal_judge:\n      provider: {provider}\n      model: {model}\n"
    "Then /goal resume to continue."
)


class GoalManager:
    """Per-session goal state + continuation decisions.

    The CLI and gateway each hold one per live session. ``evaluate_after_turn`` calls the judge and
    returns the decision dict that drives the next turn; ``next_continuation_prompt`` is the
    canonical user-role message to feed back into ``run_conversation``.
    """

    def __init__(self, session_id: str, *, default_max_turns: int = DEFAULT_MAX_TURNS):
        self.session_id = session_id
        self.default_max_turns = normalize_goal_max_turns(default_max_turns)
        self._state: Optional[GoalState] = load_goal(session_id)

    # --- introspection ------------------------------------------------

    @property
    def state(self) -> Optional[GoalState]:
        return self._state

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_goal(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def has_contract(self) -> bool:
        return self._state is not None and self._state.has_contract()

    def status_line(self) -> str:
        s = self._state
        if s is None or s.status == "cleared":
            return "No active goal. Set one with /goal <text>."
        turns = f"{_goal_budget_label(s.turns_used, s.max_turns)} turns"
        sub = f", {len(s.subgoals)} subgoal{'s' if len(s.subgoals) != 1 else ''}" if s.subgoals else ""
        con = ", contract" if self.has_contract() else ""
        gat = f", {len(s.gates)} gate{'s' if len(s.gates) != 1 else ''}" if s.gates else ""
        meta = f"{turns}{sub}{con}{gat}"
        if s.status == "active":
            if s.waiting_on_session and _session_waiting(s.waiting_on_session):
                return f"⏳ Goal (parked on {s.waiting_reason or f'session {s.waiting_on_session}'}, {meta}): {s.goal}"
            if s.waiting_on_pid and _pid_alive(s.waiting_on_pid):
                return f"⏳ Goal (parked on {s.waiting_reason or f'pid {s.waiting_on_pid}'}, {meta}): {s.goal}"
            if s.waiting_until and time.time() < s.waiting_until:
                remaining = int(s.waiting_until - time.time())
                wr = s.waiting_reason or f"{remaining}s"
                return f"⏳ Goal (parked {remaining}s — {wr}, {meta}): {s.goal}"
            return f"⊙ Goal (active, {meta}): {s.goal}"
        if s.status == "paused":
            extra = f" — {s.paused_reason}" if s.paused_reason else ""
            return f"⏸ Goal (paused, {meta}{extra}): {s.goal}"
        if s.status == "done":
            return f"✓ Goal done ({meta}): {s.goal}"
        return f"Goal ({s.status}, {meta}): {s.goal}"

    # --- mutation -----------------------------------------------------

    def _save(self) -> Optional[GoalState]:
        snapshot = getattr(self, "_evaluation_snapshot", None)
        if snapshot is not None:
            from hermes_cli.goals_evaluation import assert_goal_snapshot
            db, expected = snapshot
            assert_goal_snapshot(self.session_id, expected, db)
            return self._state
        save_goal(self.session_id, self._state)
        return self._state

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
        self._save()

    def _pause_decision(self, paused_reason: str, verdict: str, reason: str, message: str) -> Dict[str, Any]:
        self._pause_state(paused_reason)
        return _decision("paused", False, None, verdict, reason, message)

    def set(self, goal: str, *, max_turns: Optional[int] = None, contract: Optional[GoalContract] = None) -> GoalState:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal text is empty")
        self._state = GoalState(
            goal=goal, status="active", turns_used=0, created_at=time.time(), last_turn_at=0.0,
            max_turns=self.default_max_turns if max_turns is None else normalize_goal_max_turns(max_turns),
            contract=contract if contract is not None else GoalContract(),
        )
        return self._save()

    def set_contract(self, contract: GoalContract) -> Optional[GoalState]:
        """Attach or replace the completion contract on the active goal."""
        if self._state is None:
            return None
        self._state.contract = contract or GoalContract()
        return self._save()

    def pause(self, reason: str = "user-paused") -> Optional[GoalState]:
        if not self._state:
            return None
        self._state.status = "paused"
        self._state.paused_reason = reason
        self._state.clear_wait()   # a wait barrier is meaningless once paused
        return self._save()

    def resume(self, *, reset_budget: bool = True) -> Optional[GoalState]:
        if not self._state:
            return None
        self._state.status = "active"
        self._state.paused_reason = None
        self._state.consecutive_disputes = 0
        self._state.last_dispute_evidence = ""
        self._state.clear_wait()   # resuming starts fresh
        if reset_budget:
            self._state.turns_used = 0
        return self._save()

    def resume_for_user_input(self) -> bool:
        """Reactivate a goal the judge paused as BLOCKED because a real user message just
        arrived. BLOCKED means "the next step needs user input" (#100954), and that message
        IS the input — leaving the goal paused makes the user's answer run as a plain prompt
        with no judge and no continuation, while the card keeps saying "Goal paused" until
        they discover /goal resume. Only the judge's BLOCKED pause qualifies: an explicit
        /goal pause, Ctrl+C, an exhausted budget or a broken judge stay paused because the
        user must consciously choose to spend more turns there. Budget is kept, not reset
        (this is the same goal continuing). Returns True when the goal was reactivated."""
        s = self._state
        if s is None or s.status != "paused" or s.last_verdict != "blocked":
            return False
        if not (s.paused_reason or "").startswith((_BLOCKED_PAUSE_PREFIX, _LEGACY_BLOCKED_PAUSE_PREFIX)):
            return False
        self.resume(reset_budget=False)
        return True

    def clear(self) -> None:
        if self._state is None:
            return
        self._state.status = "cleared"
        self._save()
        self._state = None

    def mark_done(self, reason: str) -> None:
        if not self._state:
            return
        self._state.clear_wait()
        self._state.status = "done"
        self._state.last_verdict = "done"
        self._state.last_reason = reason
        self._save()

    # --- revisions ------------------------------------------------------

    # Changes that need an identifiable user instruction: the objective itself, dropping criteria,
    # and changing constraints. Other contract fields may be restructured by the agent; the judge
    # still holds an unauthorized revision to any earlier requirement it weakened.
    _AUTHORITY_FIELDS = ("goal", "constraints")

    def revise(self, *, reason: str, actor: str = "agent", goal: Optional[str] = None,
               contract: Optional[Dict[str, str]] = None, subgoals: Optional[List[str]] = None,
               user_quote: str = "", user_messages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Record a versioned revision of the goal, contract fields and/or subgoal list.

        ``contract`` updates only the named fields (``""`` clears one); ``subgoals`` replaces the list.
        A change to the objective or constraints, or a dropped subgoal, needs ``user_quote``: a verbatim
        excerpt (12+ chars) of a real user message in ``user_messages`` (defaults to this session's
        user messages since the goal was set). Returns ``{"ok", "error_code", "error", "revision"}``."""
        state = self._require_goal()
        reason = (reason or "").strip()
        if not reason:
            return {"ok": False, "error_code": "reason_required", "error": "a revision needs a reason"}
        before = {"goal": state.goal, **state.contract.to_dict(), "subgoals": list(state.subgoals)}
        after = dict(before)
        if goal is not None and goal.strip():
            after["goal"] = goal.strip()
        for key, value in (contract or {}).items():
            if key not in _CONTRACT_FIELDS:
                return {"ok": False, "error_code": "unknown_field", "error": f"unknown contract field: {key}"}
            after[key] = str(value or "").strip()
        if subgoals is not None:
            after["subgoals"] = [str(s).strip() for s in subgoals if str(s).strip()]
        changed = [k for k in after if after[k] != before[k]]
        if not changed:
            return {"ok": False, "error_code": "no_change", "error": "the revision changes nothing"}
        dropped = [s for s in before["subgoals"] if s not in after["subgoals"]]
        needs_authority = [k for k in self._AUTHORITY_FIELDS if k in changed] + (["subgoals"] if dropped else [])
        quote = " ".join((user_quote or "").split())
        if needs_authority and len(quote) < _REVISION_QUOTE_MIN_CHARS:
            return {"ok": False, "error_code": "user_authority_required",
                    "error": f"changing {', '.join(needs_authority)} needs user_quote: a verbatim excerpt "
                             f"({_REVISION_QUOTE_MIN_CHARS}+ chars) of the user's instruction in this session"}
        source = ""
        if quote:
            # Deterministic part: the quote must come from a real user message. Whether that message
            # authorizes this specific change is judged against the full message, shown with the revision.
            if len(quote) < _REVISION_QUOTE_MIN_CHARS:
                return {"ok": False, "error_code": "user_quote_too_short",
                        "error": f"user_quote must be at least {_REVISION_QUOTE_MIN_CHARS} characters"}
            pool = user_messages if user_messages is not None else user_messages_since(self.session_id, state.created_at)
            sources = [" ".join(m.split()) for m in pool if quote in " ".join(m.split())]
            if not sources:
                return {"ok": False, "error_code": "user_quote_not_found",
                        "error": "user_quote does not match any user message sent since the goal was set"}
            source = next((m for m in sources if len(m) <= _REVISION_SOURCE_MAX_CHARS), "")
            if not source:
                return {"ok": False, "error_code": "user_message_too_long",
                        "error": f"the quoted user message exceeds {_REVISION_SOURCE_MAX_CHARS} characters, too "
                                 "long to judge whether it authorizes this change; ask the user to state the "
                                 "change in a short message and quote that"}
        revision = {"at": time.time(), "actor": actor, "reason": reason, "user_quote": quote,
                    "user_message": source,
                    "before": {k: before[k] for k in changed}, "after": {k: after[k] for k in changed}}
        state.goal = after["goal"]
        state.contract = GoalContract.from_dict({k: after[k] for k in _CONTRACT_FIELDS})
        state.subgoals = after["subgoals"]
        state.revisions.append(revision)
        # The bar moved: an earlier dispute streak was about the superseded wording.
        state.consecutive_disputes = 0
        state.last_dispute_evidence = ""
        self._save()
        return {"ok": True, "revision": revision, "version": len(state.revisions) + 1}

    # --- /subgoal user controls ---------------------------------------

    def add_subgoal(self, text: str) -> str:
        """Append a user-added criterion; raises ``RuntimeError`` without ``has_goal()``."""
        state = self._require_goal()
        text = (text or "").strip()
        if not text:
            raise ValueError("subgoal text is empty")
        state.subgoals.append(text)
        self._save()
        return text

    def _pop_item(self, attr: str, index_1based: int):
        items = getattr(self._require_goal(), attr)
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(items):
            raise IndexError(f"index out of range (1..{len(items)})")
        removed = items.pop(idx)
        self._save()
        return removed

    def _clear_items(self, attr: str) -> int:
        state = self._require_goal()
        prev = len(getattr(state, attr))
        setattr(state, attr, [])
        self._save()
        return prev

    def remove_subgoal(self, index_1based: int) -> str:
        """Remove a subgoal by 1-based index. Returns the removed text."""
        return self._pop_item("subgoals", index_1based)

    def clear_subgoals(self) -> int:
        """Wipe all subgoals. Returns the previous count."""
        return self._clear_items("subgoals")

    def render_subgoals(self) -> str:
        """Public helper for the /subgoal slash command."""
        if self._state is None:
            return "(no active goal)"
        return self._state.render_subgoals_block() or "(no subgoals — use /subgoal <text> to add criteria)"

    # --- /goal gate quality gates ---------------------------------------

    def add_gate(self, command: str, *, timeout_seconds: Optional[int] = None, max_retries: Optional[int] = None) -> GoalGate:
        """Append a quality-gate command; raises ``RuntimeError`` without ``has_goal()``."""
        state = self._require_goal()
        command = (command or "").strip()
        if not command:
            raise ValueError("gate command is empty")
        gate = GoalGate(
            command=command,
            timeout_seconds=int(timeout_seconds) if timeout_seconds else DEFAULT_GATE_TIMEOUT_SECONDS,
            max_retries=int(max_retries) if max_retries else DEFAULT_GATE_MAX_RETRIES,
        )
        state.gates.append(gate)
        self._save()
        return gate

    def remove_gate(self, index_1based: int) -> str:
        """Remove a gate by 1-based index. Returns the removed command."""
        return self._pop_item("gates", index_1based).command

    def clear_gates(self) -> int:
        """Remove all gates. Returns the previous count."""
        return self._clear_items("gates")

    def render_gates(self) -> str:
        """Public helper for the /goal gate slash command."""
        if self._state is None:
            return "(no active goal)"
        if not self._state.gates:
            return "(no quality gates — use /goal gate add <command> to require one)"
        lines = []
        for i, g in enumerate(self._state.gates, start=1):
            status = ""
            if g.last_exit_code == 0:
                status = " ✓ passing"
            elif g.last_exit_code is not None:
                status = f" ✗ failing (exit {g.last_exit_code}, attempt {g.attempts}/{g.max_retries})"
            lines.append(f"- {i}. $ {g.command}{status}")
        return "\n".join(lines)

    def _check_gates(self) -> Optional[Dict[str, Any]]:
        """Run quality gates in order; return a decision dict on failure.

        Every eligible boundary re-executes a failed gate. A git HEAD+porcelain fingerprint used to
        replay the recorded failure when "nothing changed", but porcelain sees neither the contents
        of an untracked or already-modified file nor inputs outside the repo, so a repaired input
        was replayed as still-failing until retry exhaustion paused the goal (#110649). The
        retry cap below still bounds a genuinely stuck red suite.
        """
        state = self._state
        if state is None or not state.gates:
            return None

        gate_cwd, refusal = _gate_workspace()
        if refusal:
            return self._pause_decision(
                f"quality gates not run: {refusal}", "gate_failed", f"gates not run: {refusal}",
                f"⏸ Goal paused — quality gates not run: {refusal}. Fix the workspace or "
                f"/goal gate remove the gates, then /goal resume.",
            )
        for gate in state.gates:
            passed, exit_code, tail = run_gate(gate, cwd=gate_cwd)
            gate.last_exit_code = exit_code
            gate.last_output_tail = tail
            if passed:
                gate.attempts = 0
                continue

            gate.attempts += 1

            if gate.attempts > gate.max_retries:
                return self._pause_decision(
                    f"quality gate exhausted {gate.attempts - 1} retries: $ {gate.command}",
                    "gate_failed", f"gate exhausted retries: $ {gate.command}",
                    f"⏸ Goal paused — quality gate still failing after "
                    f"{gate.max_retries} retries: $ {gate.command} "
                    f"(exit {exit_code}). Fix it manually or /goal gate remove it, "
                    f"then /goal resume.",
                )

            self._save()
            prompt = CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE.format(
                goal=state.goal, command=gate.command, exit_code=exit_code, attempt=gate.attempts,
                max_retries=gate.max_retries, output=tail or "(no output)",
            )
            return _decision(
                "active", True, prompt, "gate_failed",
                f"gate failed (exit {exit_code}): $ {gate.command}",
                f"✗ Quality gate failed ({_goal_budget_label(state.turns_used, state.max_turns)} turns, "
                f"attempt {gate.attempts}/{gate.max_retries}): $ {gate.command}",
            )

        self._save()
        return None

    # --- /goal wait barrier -------------------------------------------

    def _park(self, reason: str, **barrier) -> GoalState:
        state = self._require_active()
        # Re-parking is not progress: keep the last notice key so an unchanged judge WAIT
        # remains silent, while clearing barrier fields before applying the new target.
        state.clear_wait(preserve_notice_key=True)
        for k, v in barrier.items():
            setattr(state, k, v)
        state.waiting_reason = (reason or "").strip() or None
        state.waiting_since = time.time()
        return self._save()

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

    def wait_on_session(self, session_id: str, reason: str = "") -> GoalState:
        """Park on a process_registry session's OWN trigger: exit OR ``watch_patterns`` match. The
        right barrier for a long-lived watcher/poller that signals mid-run and may never exit."""
        self._require_active()
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id must be a non-empty string")
        return self._park(reason, waiting_on_session=session_id)

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

    def stop_waiting(self) -> bool:
        """Clear any active wait barrier (pid / session / time). Returns True if one was cleared."""
        s = self._state
        if s is None or (s.waiting_on_pid is None and s.waiting_on_session is None and not s.waiting_until):
            return False
        s.clear_wait()
        self._save()
        return True

    def clear_lifted_wait(self, waiting_since: float) -> bool:
        """Clear the barrier an idle surface just resumed, only if it is still that same wait
        (matched by ``waiting_since`` on the durable row). The resumed turn may already have finished
        and parked again; a blind ``stop_waiting`` would erase that newer barrier."""
        cleared, current = clear_goal_wait_if_since(self.session_id, waiting_since)
        if current is not None:
            self._state = current
        return cleared

    def is_parked(self) -> bool:
        """True when an active goal carries a wait barrier, whether or not it still holds."""
        s = self._state
        return bool(s is not None and s.status == "active" and (
            s.waiting_on_pid is not None or s.waiting_on_session is not None or s.waiting_until))

    def _barrier_holds(self) -> bool:
        """Pure check: True iff a barrier is set AND not yet satisfied (never mutates state)."""
        s = self._state
        if s is None:
            return False
        if s.waiting_on_session is not None:
            still = _session_waiting(s.waiting_on_session)
        elif s.waiting_on_pid is not None:
            still = _pid_alive(s.waiting_on_pid)
        elif s.waiting_until:
            still = time.time() < s.waiting_until
            if still and s.waiting_on_delegations > 0:
                # Set because of live delegations: lift the moment one of them returned.
                live = count_active_delegations(self.session_id)
                if live < s.waiting_on_delegations:
                    still = False
        else:
            return False
        if still and s.waiting_since and s.waiting_until == 0.0 and time.time() - s.waiting_since > _MAX_BARRIER_WAIT_S:
            logger.info("goal %s: wait barrier on %s exceeded %ds; resuming judging",
                        self.session_id, s.waiting_on_session or s.waiting_on_pid, _MAX_BARRIER_WAIT_S)
            still = False
        return still

    def is_waiting(self) -> bool:
        """True iff a barrier is set AND not yet satisfied. A satisfied barrier is cleared here
        (lazy auto-clear) so the next evaluation resumes normal judging. A pid/session barrier
        also expires after ``_MAX_BARRIER_WAIT_S``: a watcher or poller that never exits would
        otherwise park the goal indefinitely (one run sat 3 h 22 min on a poller that outlived
        the work it was polling)."""
        s = self._state
        if s is None or not (s.waiting_on_pid is not None or s.waiting_on_session is not None or s.waiting_until):
            return False
        still = self._barrier_holds()
        if not still:
            self.stop_waiting()
        return still

    def lifted_barrier_prompt(self) -> Optional[str]:
        """Continuation prompt for a parked goal whose barrier has lifted, else None. Pure: the
        caller clears the barrier (``stop_waiting``) only after the prompt was admitted, so a failed
        injection is retried instead of leaving an active goal with nothing left to drive it.

        Idle surfaces (CLI idle hook, gateway wakeup ticker) call this because the post-turn judge
        only runs after a turn: a barrier whose completion notice never arrives (a gateway restart
        killed the process, notify_on_complete was off, a timed wait elapsed) otherwise parks the
        goal until an unrelated message happens to arrive."""
        if not self.is_parked() or self._barrier_holds():
            return None
        if self._state.waiting_on_session and _completion_notice_pending(self._state.waiting_on_session):
            return None
        prompt = self.next_continuation_prompt()
        if not prompt:
            return None
        note = _barrier_lift_note(self._state)
        return f"{prompt}\n\n{note}" if note else prompt

    # --- the main entry point called after every turn -----------------

    def _wait_notice_key(self, state: GoalState) -> str:
        """Stable identity for the current wait barrier, excluding elapsed presentation text."""
        reason = state.waiting_reason or ""
        if state.waiting_on_session is not None:
            target = f"session:{state.waiting_on_session}"
        elif state.waiting_on_pid is not None:
            target = f"pid:{state.waiting_on_pid}"
        else:
            target = f"seconds:{state.waiting_seconds}:delegations:{state.waiting_on_delegations}"
        return f"{target}|reason:{reason}"

    @staticmethod
    def _waiting_target(state: GoalState) -> str:
        if state.waiting_on_session is not None:
            return f"session {state.waiting_on_session}"
        if state.waiting_on_pid is not None:
            return f"pid {state.waiting_on_pid}"
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

    def _waiting_decision(self, state: GoalState) -> Dict[str, Any]:
        tgt = self._waiting_target(state)
        reason = state.waiting_reason or tgt
        return self._wait_notice_decision(
            state, verdict="waiting",
            message=f"⏳ Goal parked — waiting on {tgt}: {reason}",
        )

    def _apply_wait_directive(
        self, wait_directive: Dict[str, Any], reason: str, *, active_delegations: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Judge said WAIT: set the barrier and park. The counted turn stands (the judge ran) but no
        continuation fires; the loop resumes once the barrier clears. ``None`` = the barrier is
        unobservable here, so the caller continues instead."""
        state = self._require_active()
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
                # window where the pid exits between a pre-check and the park.
                logger.info("goal judge: wait_on_pid %s is not alive on this host; continuing", pid)
                return None
        else:
            self.wait_for_seconds(int(wait_directive["seconds"]), reason=reason, on_delegations=active_delegations)
            tgt = f"{wait_directive['seconds']}s"
        return self._wait_notice_decision(
            state, verdict="wait",
            message=f"⏳ Goal parked (judge) — waiting on {tgt}: {reason}",
        )

    def _budget_pause(self, state: GoalState, verdict: str, reason: str, note: str = "") -> Dict[str, Any]:
        return self._pause_decision(
            f"turn budget exhausted ({_goal_budget_label(state.turns_used, state.max_turns)})", verdict, reason,
            f"⏸ Goal paused — {_goal_budget_label(state.turns_used, state.max_turns)} turns used{note}. "
            "Use /goal resume to keep going, or /goal clear to stop.",
        )

    def evaluate_after_turn(
        self, last_response: str, *, user_initiated: bool = True,
        background_processes: Optional[List[Dict[str, Any]]] = None,
        active_delegations: int = 0,
        evidence_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Evaluate an isolated snapshot and atomically commit against its durable state."""
        from hermes_cli.goals_evaluation import evaluate_goal_snapshot
        return evaluate_goal_snapshot(
            self, last_response, user_initiated=user_initiated,
            background_processes=background_processes, active_delegations=active_delegations,
            evidence_session_id=evidence_session_id,
        )

    def _evaluate_after_turn(
        self, last_response: str, *, user_initiated: bool = True,
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
        if state is None or state.status != "active":
            return _decision(state.status if state else None, False, None, "inactive", "no active goal", "")

        # Parked on a live process or an unexpired deadline: quiesce without burning a turn.
        if self.is_waiting():
            return self._waiting_decision(state)

        state.turns_used += 1
        state.last_turn_at = time.time()

        # Gates run BEFORE the judge: a failing gate is deterministic evidence the goal is not done,
        # so the judge is skipped and the gate's output drives the next turn (same turn budget).
        gate_decision = self._check_gates()
        if gate_decision is not None:
            if gate_decision.get("should_continue") and state.max_turns > 0 and state.turns_used >= state.max_turns:
                return self._budget_pause(state, "gate_failed", gate_decision.get("reason", ""), note=" (a quality gate is still failing)")
            return gate_decision

        evidence = collect_goal_evidence(evidence_session_id or self.session_id, since=state.created_at)
        # Gates that just passed are deterministic evidence too; before this they only vetoed DONE.
        now = time.time()
        evidence += [
            {"tool": "quality gate", "call": f"$ {g.command}", "timestamp": now,
             "output": f"exit {g.last_exit_code} (passed)\n{_tail((g.last_output_tail or '').strip(), _EVIDENCE_OUTPUT_CHARS)}".strip()}
            for g in state.gates
        ]
        citations = resolve_cited_evidence(evidence_session_id or self.session_id, last_response,
                                           since=state.created_at)
        verdict, reason, parse_failed, wait_directive, transport_failed = judge_goal(
            state.goal, last_response, subgoals=state.subgoals or None, background_processes=background_processes,
            contract=state.contract if state.has_contract() else None, active_delegations=active_delegations,
            evidence=evidence or None, citations=citations, revisions_block=state.render_revisions_block(),
        )
        state.last_verdict = verdict
        state.last_reason = reason
        # Parse failures reset on any usable reply INCLUDING transport errors, so a flaky network
        # doesn't trip the auto-pause meant for bad judge models; transport failures are counted
        # separately because persistent API errors (401, DNS) mean a broken config.
        state.consecutive_parse_failures = state.consecutive_parse_failures + 1 if parse_failed else 0
        state.consecutive_transport_failures = state.consecutive_transport_failures + 1 if transport_failed else 0
        disputed = verdict == "continue" and bool((wait_directive or {}).get("disputed"))
        # A dispute counts toward the stall breaker unless the reply cites a recorded result no
        # earlier dispute in this streak cited: new proof means converging, rewording is not.
        current = set(citations.get("evidence_ids") or [])
        seen = {i for i in state.last_dispute_evidence.split(",") if i}
        if not disputed:
            state.consecutive_disputes = 0
            state.last_dispute_evidence = ""
        else:
            fresh = current - seen
            state.consecutive_disputes = 1 if (state.consecutive_disputes and fresh) else state.consecutive_disputes + 1
            state.last_dispute_evidence = ",".join(sorted(seen | current, key=_evidence_id_order)[-_DISPUTE_SEEN_MAX:])

        if verdict == "wait" and wait_directive:
            parked = self._apply_wait_directive(
                wait_directive, reason, active_delegations=active_delegations,
            )
            if parked is not None:
                return parked

        # BLOCKED is NOT done: pause for missing user input, an external prerequisite, or
        # an impossible goal. A recoverable dependency must never be called unachievable.
        if verdict == "blocked":
            return self._pause_decision(
                f"{_BLOCKED_PAUSE_PREFIX}{reason}", "blocked", reason,
                f"⏸ Goal blocked — paused: {reason} If input is needed, supply it to continue; "
                "use /goal set to re-scope or /goal resume to retry.",
            )

        if verdict == "done":
            state.status = "done"
            self._save()
            return _decision("done", False, None, "done", reason, f"✓ Goal achieved: {reason}")

        # Persistent judge failures (API unreachable / unparseable output) auto-pause and point at the
        # goal_judge config so a broken judge can't burn the whole turn budget.
        n_tx, n_parse = state.consecutive_transport_failures, state.consecutive_parse_failures
        if n_tx >= DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES:
            return self._pause_decision(
                f"judge API unreachable {n_tx} turns in a row (check auxiliary.goal_judge provider/key in config.yaml)",
                "continue", reason,
                f"⏸ Goal paused — judge API returned errors ({n_tx} turns). Check the goal_judge provider/key in "
                + _JUDGE_CONFIG_HINT.format(provider="deepseek", model="deepseek-flash"),
            )
        if n_parse >= DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES:
            return self._pause_decision(
                f"judge model returned unparseable output {n_parse} turns in a row", "continue", reason,
                f"⏸ Goal paused — the judge model ({n_parse} turns) isn't returning the required JSON verdict. "
                "Route the judge to a stricter model in "
                + _JUDGE_CONFIG_HINT.format(provider="openrouter", model="google/gemini-3-flash-preview"),
            )

        # Stall breaker: the agent keeps asserting completion and the judge keeps rejecting it.
        # Another continuation only yields a restated claim, so the user decides.
        if state.consecutive_disputes >= DEFAULT_MAX_CONSECUTIVE_DISPUTES:
            return self._pause_decision(
                f"{_DISPUTED_PAUSE_PREFIX}{reason}", "disputed", reason,
                f"⏸ Goal paused — the agent reports the goal is complete, but the judge disagreed "
                f"{state.consecutive_disputes} turns in a row without new evidence: {reason} "
                "Use /goal clear if it is done, /goal resume to keep going, or tell the agent "
                "which criterion no longer applies.",
            )

        if state.max_turns > 0 and state.turns_used >= state.max_turns:
            return self._budget_pause(state, "continue", reason)

        self._save()
        return _decision(
            "active", True, self.next_continuation_prompt(), "continue", reason,
            f"↻ Continuing toward goal ({_goal_budget_label(state.turns_used, state.max_turns)}): {reason}",
        )

    def next_continuation_prompt(self) -> Optional[str]:
        s = self._state
        if not s or s.status != "active":
            return None
        prompt = self._current_continuation_prompt(s)
        if s.revisions:
            prompt += CONTINUATION_REVISIONS_TEMPLATE.format(revision_lines=s.render_revisions_block())
        return prompt

    @staticmethod
    def _current_continuation_prompt(s: "GoalState") -> str:
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
KANBAN_GOAL_CONTINUATION_TEMPLATE = (
    "[Continuing toward this kanban task — judge says it is not done yet]\n"
    "Reason: {reason}\n\n"
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
) -> Dict[str, Any]:
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

    def _result(outcome: str, reason: str) -> Dict[str, Any]:
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

        # The between-turns judge runs outside any agent turn: bind the per-task relay-affinity
        # scope (same shape as the handoff gates) so the relay does not reject the call (#113669).
        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task_id}")
        try:
            verdict, reason, _parse_failed, _wait, _transport_failed = judge_goal(goal_text, last_response)
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
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
            last_response = run_turn(prompt) or ""
        except Exception as exc:
            _log(f"kanban goal loop: run_turn failed ({exc}); stopping")
            return _result("stopped", f"run_turn error: {type(exc).__name__}")
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
