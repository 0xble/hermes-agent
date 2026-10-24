"""Recurring in-session wakeups — the /loop command (Claude Code parity).

A loop stops when the agent ends a wakeup reply with ``LOOP_COMPLETE`` on its own line, when
``--times N`` ticks have fired, when the ``--until`` judge rules the condition met, or when the
``loops.max_ticks`` backstop pauses it. State lives in SessionDB ``state_meta`` (same contract as
``hermes_cli/goals.py``); CLI, gateway, and TUI all drive it through :class:`LoopManager`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, fields, asdict
from typing import Any, Dict, List, Optional, Tuple

from hermes_cli.heartbeat import HEARTBEAT_PROMPT_PREFIX, SILENCE_MARKER, is_intentional_silence_response
from hermes_cli.goals import _AUTONOMOUS_REPLY_RULES, _NO_PROGRESS_BACKOFF_S
from hermes_cli.loops_revisions import LoopRevisionsMixin
from hermes_cli.lifecycle_snapshot import commit_and_publish_snapshot, snapshot_control

logger = logging.getLogger(__name__)


# Floor for fixed intervals. Claude Code allows 30s; anything tighter is almost always an
# accident that burns tokens polling unchanged state. Config loops.min_interval_seconds (clamped ≥ 5).
DEFAULT_MIN_INTERVAL_SECONDS = 30
# Backstop tick budget so an unattended loop can't run forever. 0 = unlimited; config loops.max_ticks.
DEFAULT_MAX_TICKS = 100
# Self-paced mode: start at the configured floor, then use the same quiet no-progress ladder as
# goals while replies are unchanged; snap back to the floor on any change.
DEFAULT_SELF_PACED_FLOOR_SECONDS = 60
DEFAULT_SELF_PACED_CEILING_SECONDS = _NO_PROGRESS_BACKOFF_S[-1]

# Completion sentinel the wakeup prompt teaches the agent to emit.
LOOP_COMPLETE_MARKER = "LOOP_COMPLETE"
# Marker on its own line, tolerating surrounding whitespace / trailing punctuation.
_LOOP_COMPLETE_RE = re.compile(
    r"(?im)^\s*" + re.escape(LOOP_COMPLETE_MARKER) + r"\s*[.!]?\s*$"
)
# Interval token: 30s / 5m / 2h / 1h30m (compound units allowed, at least one).
_INTERVAL_TOKEN_RE = re.compile(
    r"^(?=\d)(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE
)


WAKEUP_PROMPT_PREFIX = "[/loop wakeup #"
# Shared wakeup contract. A wakeup is machinery, not a conversation: the user hears from the loop
# only when something new needs them, so a no-change tick ends with the bare silence marker.
# The silence paragraph is the goal contract's verbatim. Its update and done wording is goal
# specific (a goal claims done with an Evidence section), so loops carry their own: every visible
# loop reply, including the completing one, is one or two short lines.
_WAKEUP_SILENCE_RULE = _AUTONOMOUS_REPLY_RULES.split("\n", 1)[0] + "\n"
_WAKEUP_REPLY_RULES = (
    _WAKEUP_SILENCE_RULE
    + "When something changed, reply in one or two short lines: the new fact, and the user's "
    "needed action if any. No Evidence section, recap, or notes on what you did not do. Being "
    "blocked or needing input still requires a visible reply.\n"
)
_WAKEUP_DONE_REPLY = (
    "say so in the same one or two short lines, then put "
    f"{LOOP_COMPLETE_MARKER} on its own line; that stops the loop. "
)
_WAKEUP_REVISE_RULE = (
    "If the cadence, run count, or stop condition no longer fits, revise the loop with the "
    "loop_set tool (action=revise) instead of stopping it."
)
WAKEUP_PROMPT_TEMPLATE = (
    f"{WAKEUP_PROMPT_PREFIX}{{tick}}{{cadence}}]\n"
    "Recurring task: {prompt}\n\n"
    "This is an automatic wakeup from the /loop. "
    + _WAKEUP_REPLY_RULES
    + "If the task is complete, no longer applicable, or the thing you were watching has "
    "finished, "
    + _WAKEUP_DONE_REPLY
    + _WAKEUP_REVISE_RULE
)

WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE = (
    f"{WAKEUP_PROMPT_PREFIX}{{tick}}{{cadence}}]\n"
    "Recurring task: {prompt}\n\n"
    "Stop condition: {until}\n\n"
    "This is an automatic wakeup from the /loop. "
    + _WAKEUP_REPLY_RULES
    + "If the stop condition is met, or the task is no longer applicable, "
    + _WAKEUP_DONE_REPLY
    + _WAKEUP_REVISE_RULE
)

# Earlier wordings. Stored rows still carry them and legacy rows have no display_kind, so the
# generated-turn classifier (agent/synthetic_prompt.py) keeps recognizing every one; never render
# these. Each pair is (no-until, with-until), oldest first after the pre-silence pair below.
_SILENCE_CONTRACT_WAKEUP_REPLY_RULES = (
    "Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. "
    "If nothing new and material happened since your last visible update and nothing needs the "
    f"user's action, reply with exactly {SILENCE_MARKER} and nothing else. That includes plain "
    "acknowledgements and anything that changes nothing for the user. Never send status like "
    "\"still waiting\", \"nothing new\", or what you did not do.\n"
    "If something did change, reply in one or two short lines with only the new fact or the "
    "action the user needs to take.\n"
)


def _evidence_wakeup_templates(reply_rules: str) -> Tuple[str, str]:
    return (
        f"{WAKEUP_PROMPT_PREFIX}{{tick}}{{cadence}}]\n"
        "Recurring task: {prompt}\n\n"
        "This is an automatic wakeup from the /loop. "
        + reply_rules
        + "If the task is complete, no longer applicable, or the thing you were watching has "
        f"finished, say so briefly with the evidence and end with {LOOP_COMPLETE_MARKER} on its own "
        "line; that stops the loop. "
        + _WAKEUP_REVISE_RULE,
        f"{WAKEUP_PROMPT_PREFIX}{{tick}}{{cadence}}]\n"
        "Recurring task: {prompt}\n\n"
        "Stop condition: {until}\n\n"
        "This is an automatic wakeup from the /loop. "
        + reply_rules
        + "If the stop condition is met, or the task is no longer applicable, say so briefly with "
        f"the evidence and end with {LOOP_COMPLETE_MARKER} on its own line; that stops the loop. "
        + _WAKEUP_REVISE_RULE,
    )


# The first silence-contract wording, then the wording shared verbatim with goal continuations.
(
    _SILENCE_CONTRACT_WAKEUP_PROMPT_TEMPLATE,
    _SILENCE_CONTRACT_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
) = _evidence_wakeup_templates(_SILENCE_CONTRACT_WAKEUP_REPLY_RULES)
(
    _GOAL_RULES_WAKEUP_PROMPT_TEMPLATE,
    _GOAL_RULES_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
) = _evidence_wakeup_templates(_AUTONOMOUS_REPLY_RULES)

# The original wording, before loop revisions added the revise sentence.
_ORIGINAL_WAKEUP_PROMPT_TEMPLATE = (
    f"{WAKEUP_PROMPT_PREFIX}{{tick}}{{cadence}}]\n"
    "Recurring task: {prompt}\n\n"
    "This is an automatic wakeup from the /loop the user set. Perform the "
    "task now against the CURRENT state (re-check files, processes, or "
    "services fresh — do not assume anything from earlier iterations still "
    "holds). Report concisely what you found or did this iteration.\n"
    "If the task is now complete, no longer applicable, or the thing you "
    "were watching has finished, say so and end your reply with "
    f"{LOOP_COMPLETE_MARKER} on its own line — that stops the loop."
)
_ORIGINAL_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE = (
    f"{WAKEUP_PROMPT_PREFIX}{{tick}}{{cadence}}]\n"
    "Recurring task: {prompt}\n\n"
    "Stop condition: {until}\n\n"
    "This is an automatic wakeup from the /loop the user set. Perform the "
    "task now against the CURRENT state (re-check files, processes, or "
    "services fresh — do not assume anything from earlier iterations still "
    "holds). Report concisely what you found or did this iteration, and "
    "show concrete evidence of the stop condition's status.\n"
    "If the stop condition is met, or the task is no longer applicable, say "
    f"so and end your reply with {LOOP_COMPLETE_MARKER} on its own line — "
    "that stops the loop."
)

# Wording before the silence contract.
_PREVIOUS_WAKEUP_PROMPT_TEMPLATE = (
    f"{WAKEUP_PROMPT_PREFIX}{{tick}}{{cadence}}]\n"
    "Recurring task: {prompt}\n\n"
    "This is an automatic wakeup from the /loop the user set. Perform the "
    "task now against the CURRENT state (re-check files, processes, or "
    "services fresh — do not assume anything from earlier iterations still "
    "holds). Report concisely what you found or did this iteration.\n"
    "If the task is now complete, no longer applicable, or the thing you "
    "were watching has finished, say so and end your reply with "
    f"{LOOP_COMPLETE_MARKER} on its own line — that stops the loop. "
    "If the cadence, run count, or stop condition no longer fits, revise "
    "the loop with the loop_set tool (action=revise) instead of stopping it."
)
_PREVIOUS_WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE = (
    f"{WAKEUP_PROMPT_PREFIX}{{tick}}{{cadence}}]\n"
    "Recurring task: {prompt}\n\n"
    "Stop condition: {until}\n\n"
    "This is an automatic wakeup from the /loop the user set. Perform the "
    "task now against the CURRENT state (re-check files, processes, or "
    "services fresh — do not assume anything from earlier iterations still "
    "holds). Report concisely what you found or did this iteration, and "
    "show concrete evidence of the stop condition's status.\n"
    "If the stop condition is met, or the task is no longer applicable, say "
    f"so and end your reply with {LOOP_COMPLETE_MARKER} on its own line — "
    "that stops the loop. If the cadence, run count, or stop condition no "
    "longer fits, revise the loop with the loop_set tool (action=revise) "
    "instead of stopping it."
)


def is_quiet_wakeup_prompt(text: Any) -> bool:
    """Whether ``text`` is an autonomous wakeup prompt whose bare silence marker is intentional.

    Surfaces without the gateway's machinery (CLI, TUI, Desktop) use this to hide the marker.
    Quality-gate-failed goal turns are excluded because they must always report the failure.
    """
    if not isinstance(text, str):
        return False
    from hermes_cli.goals import GOAL_CONTINUATION_PREFIX
    return text.startswith((WAKEUP_PROMPT_PREFIX, HEARTBEAT_PROMPT_PREFIX, GOAL_CONTINUATION_PREFIX))


def _is_silence_reply(response: Any) -> bool:
    return is_intentional_silence_response(response)


def parse_interval_token(token: str) -> Optional[int]:
    """Total seconds for ``30s``/``5m``/``2h``/``1h30m``, else None.

    A bare number is NOT an interval (it collides with prompt text like ``/loop 3 things``).
    """
    m = _INTERVAL_TOKEN_RE.match(token.strip()) if token else None
    if not m:
        return None
    h, mnt, s = (int(g) if g else 0 for g in m.groups())
    total = h * 3600 + mnt * 60 + s
    return total if total > 0 else None


def parse_loop_args(text: str) -> dict[str, Any]:
    """Parse ``/loop [interval] <prompt> [--times N] [--until ...]``.

    Returns ``{"interval_seconds": int|None, "prompt", "times", "until", "error"}``;
    ``interval_seconds`` None means self-paced, ``error`` is set for unusable input.
    """
    raw = (text or "").strip()
    result: dict[str, Any] = {"interval_seconds": None, "prompt": "", "times": 0, "until": "", "error": None}
    if not raw:
        return {**result, "error": "empty"}

    # Pull trailing flags first so an interval-looking token inside the --until clause can't
    # confuse the front parse. --until consumes to end-of-line (or to a following --times).
    times, until = 0, ""
    m_times = re.search(r"\s--times\s+(\S+)", raw)
    if m_times:
        try:
            times = int(m_times.group(1))
            if times < 1:
                raise ValueError
        except ValueError:
            return {**result, "error": f"--times expects a positive integer, got {m_times.group(1)!r}"}
        raw = (raw[: m_times.start()] + raw[m_times.end():]).strip()

    m_until = re.search(r"\s--until\s+(.+)$", raw, re.DOTALL)
    if m_until:
        until = m_until.group(1).strip()
        raw = raw[: m_until.start()].strip()

    # Leading "every" sugar: /loop every 5m <prompt>
    tokens = raw.split(None, 1)
    if tokens and tokens[0].lower() == "every" and len(tokens) > 1:
        raw = tokens[1]
        tokens = raw.split(None, 1)

    interval = parse_interval_token(tokens[0]) if tokens else None
    if interval is not None:
        raw = tokens[1].strip() if len(tokens) > 1 else ""

    if not raw:
        return {**result, "error": "missing prompt (usage: /loop [interval] <prompt>)"}
    return {**result, "interval_seconds": interval, "prompt": raw, "times": times, "until": until}


def format_interval(seconds: float) -> str:
    """Render seconds as a compact human interval (``90`` → ``1m30s``)."""
    h, rem = divmod(int(max(0, round(seconds))), 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h}h"] if h else []
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return "".join(parts)


def _loops_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        section = (load_config() or {}).get("loops") or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _config_int(key: str, default: int, floor: int) -> int:
    """``loops.<key>`` as an int clamped to ``floor``; ``default`` on any bad value."""
    try:
        return max(floor, int(_loops_config().get(key, default)))
    except Exception:
        return default


def min_interval_seconds() -> int:
    return _config_int("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS, 5)


def max_ticks_default() -> int:
    return _config_int("max_ticks", DEFAULT_MAX_TICKS, 0)


def self_paced_floor_seconds() -> int:
    return _config_int("self_paced_floor_seconds", DEFAULT_SELF_PACED_FLOOR_SECONDS, 10)


def self_paced_ceiling_seconds() -> int:
    floor = self_paced_floor_seconds()
    return _config_int("self_paced_ceiling_seconds", max(floor, DEFAULT_SELF_PACED_CEILING_SECONDS), floor)


@dataclass
class LoopState:
    """Serializable /loop state stored per session."""

    prompt: str
    status: str = "active"            # active | paused | done | cleared
    mode: str = "interval"            # interval | self_paced
    interval_seconds: float = 0.0     # fixed cadence (mode == "interval")
    current_delay: float = 0.0        # live cadence (self-paced backoff)
    times: int = 0                    # user cap (--times N); 0 = none
    until: str = ""                   # judged stop condition; "" = none
    max_ticks: int = DEFAULT_MAX_TICKS  # config backstop; 0 = unlimited
    ticks_fired: int = 0
    created_at: float = 0.0
    # Monotonic control fence, owned by ``save_loop``: any write that changes the status or loop
    # instance, or (outside tick lifecycle) clears/replaces the in-flight tick, bumps it so an
    # admitted tick cannot cross a control boundary during the executor return gap. Callers never
    # set it; a cached copy can never write an older value back over the stored row.
    control_generation: int = 0
    last_fired_at: float = 0.0
    next_due_at: float = 0.0
    # True between "wakeup injected" and "that turn's response evaluated": stops a tick from
    # double-firing mid-turn and tells the post-turn hook the turn that just ended was ours.
    awaiting_response: bool = False
    # Admission starts the tick in the same stored-row CAS that claims it. Legacy phases also
    # have unknown outcomes and are skipped on ownerless recovery, never refunded or replayed.
    claim_phase: str = ""  # started | empty (legacy/unknown)
    # Gateway session run generation captured by the admission CAS. This is a JSON state field,
    # not a database schema column, so independent processes compare the same durable row.
    run_generation: int = 0
    # Definition version the in-flight tick fired with; completion judges against it, so a revise
    # while the turn runs supersedes that turn's stop verdict even for a freshly loaded manager.
    fired_version: int = 0
    last_response_digest: str = ""    # self-paced change detection
    # Last fired tick count that consumed a trusted external wake; at most one early wake per settled tick.
    last_external_wake_tick: int = -1
    # Non-blocking overlap guidance returned by loop_set results when a goal already owns this session.
    goal_warning: Optional[str] = None
    # Shared no-progress ladder index. It advances on unchanged self-paced replies and one-shot
    # --until blocks, and resets when a reply changes or a user revises/resumes the loop.
    backoff_level: int = 0
    paused_reason: Optional[str] = None
    last_stop_reason: Optional[str] = None
    # Gateway routing (platform / chat_id / chat_type / thread_id) captured at creation so the
    # idle watcher can inject ticks into the right chat. Empty for CLI/TUI (own schedulers).
    route: dict[str, str] = field(default_factory=dict)
    # Versioned agent-authored changes; old rows omit this field and load as an empty list.
    revisions: List[Dict[str, Any]] = field(default_factory=list)
    mutation_id: str = ""  # durable-write token, including same-value lifecycle changes
    tick_id: str = ""  # survives revisions, changes on every new claim

    @property
    def version(self) -> int:
        return len(self.revisions) + 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> LoopState:
        data = json.loads(raw)
        route = data.get("route")
        revisions = data.get("revisions", [])
        kwargs: dict[str, Any] = {
            "prompt": data.get("prompt", ""),
            "status": data.get("status", "active"),
            "mode": data.get("mode", "interval"),
            "paused_reason": data.get("paused_reason"),
            "last_stop_reason": data.get("last_stop_reason"),
            "goal_warning": data.get("goal_warning"),
            "route": route if isinstance(route, dict) else {},
            "revisions": revisions if isinstance(revisions, list) else [],
        }
        # Remaining scalar fields: missing key -> dataclass default; present-but-falsy -> type zero.
        # ``from __future__ import annotations`` leaves ``f.type`` as a string, so list fields must
        # be handled explicitly rather than passed through the scalar cast table.
        casts = {"str": str, "int": int, "float": float, "bool": bool}
        for f in fields(cls):
            if f.name in kwargs:
                continue
            if f.type not in casts:
                continue
            kwargs[f.name] = casts[f.type](data.get(f.name, f.default) or casts[f.type]())
        return cls(**kwargs)

    def cadence_label(self) -> str:
        if self.mode == "self_paced":
            live = f", currently {format_interval(self.current_delay)}" if self.current_delay else ""
            return f"self-paced{live}"
        return f"every {format_interval(self.interval_seconds)}"

    def remaining_label(self) -> str:
        if self.status != "active":
            return ""
        remaining = self.next_due_at - time.time()
        return "due now" if remaining <= 0 else f"next in {format_interval(remaining)}"


_META_PREFIX = "loop:"
# Keyed by (store home, session) so multiplexed profiles never share a fence. Retired loops drop
# out; the stored row stays authoritative for them and the sync fence treats absence as stale.
_CONTROL_GENERATIONS: Dict[Tuple[str, str], int] = {}


def _meta_key(session_id: str) -> str:
    return f"{_META_PREFIX}{session_id}"


def _control_generation_key(session_id: str) -> Tuple[str, str]:
    from hermes_constants import hermes_home_key

    return hermes_home_key(), session_id


def _remember_control_generation(session_id: str, state: Optional[LoopState]) -> None:
    if not session_id or state is None:
        return
    key = _control_generation_key(session_id)
    if state.status == "cleared":
        _CONTROL_GENERATIONS.pop(key, None)
        return
    current = _CONTROL_GENERATIONS.get(key, 0)
    _CONTROL_GENERATIONS[key] = max(current, int(getattr(state, "control_generation", 0) or 0))


def loop_control_generation(session_id: str, default: Optional[int] = 0) -> Optional[int]:
    """Return the process-local fence for synchronous gateway execution-entry checks."""
    return _CONTROL_GENERATIONS.get(_control_generation_key(session_id), default)


def _get_session_db() -> Optional[Any]:
    """The goals module's cached SessionDB, so goals/loops/heartbeats share one connection and
    its off-loop bootstrap (a cold cache on the loop thread never runs ``SessionDB()`` inline).

    The previous copy here did, which froze the loop for the init duration and dropped the first ``loop:*``
    write (the /goal bug class, #88965).
    """
    try:
        from hermes_cli.goals import _get_session_db as _goals_db
    except Exception as exc:  # pragma: no cover
        logger.debug("LoopManager: SessionDB bootstrap failed (%s)", exc)
        return None
    return _goals_db()


def _db_op(label: str, fn, default=None):
    """Run one SessionDB call; any error is logged at debug and yields ``default``."""
    try:
        return fn()
    except Exception as exc:
        logger.debug("LoopManager: %s failed: %s", label, exc)
        return default


def _parse_state(raw: str, session_id: str = "") -> Optional[LoopState]:
    """``LoopState`` from stored JSON; None (warning when *session_id* given) on corrupt data."""
    try:
        return LoopState.from_json(raw)
    except Exception as exc:
        if session_id:
            logger.warning("LoopManager: could not parse stored loop for %s: %s", session_id, exc)
        return None


def load_loop(session_id: str, *, cursor=None) -> Optional[LoopState]:
    """Load the loop for a session, or None if none exists."""
    db = _get_session_db() if session_id else None
    if db is None:
        return None
    if cursor is not None:
        row = cursor.execute("SELECT value FROM state_meta WHERE key = ?", (_meta_key(session_id),)).fetchone()
        raw = row[0] if row else None
    else:
        raw = _db_op("get_meta", lambda: db.get_meta(_meta_key(session_id)))
    state = _parse_state(raw, session_id) if raw else None
    _remember_control_generation(session_id, state)
    return state


def _next_control_generation(session_id: str, stored: Optional[LoopState], state: LoopState, *,
                             tick_lifecycle: bool) -> int:
    """The control fence *state* must carry when it replaces *stored* (the single bump rule).

    The stored row (and this process's fence) is authoritative, never the writer's cached copy,
    so a stale manager cannot regress the fence. The fence advances when the write changes the
    status or the loop instance (``created_at``): set, replace, pause, resume, clear, stop
    verdicts, recovery budget stops and session-reset retirement. Outside the tick lifecycle's own
    claim/settlement writes it also advances when the in-flight tick is cleared or replaced. A
    definition-only revise keeps the in-flight tick by design and so leaves the fence alone.
    """
    current = max(loop_control_generation(session_id),
                  int(getattr(stored, "control_generation", 0) or 0))
    if stored is None:
        return max(current, int(state.control_generation or 0))
    changed = stored.status != state.status or stored.created_at != state.created_at
    if not tick_lifecycle and stored.awaiting_response and (
        not state.awaiting_response or stored.ticks_fired != state.ticks_fired
    ):
        changed = True
    return current + 1 if changed else current


def save_loop(session_id: str, state: Optional[LoopState] = None, *, cursor=None,
              tick_lifecycle: bool = False, apply=None):
    """Write a loop or apply a command to the current row under the same control fence.

    Command callbacks validate and edit only their owned fields on the live row. They may
    be replayed on lock contention and must not mutate a manager's cached state. Return the
    persisted row and command result only after a successful write; cursor owners see errors.
    """
    if not session_id:
        return None
    db = _get_session_db()
    if db is None:
        if cursor is not None:
            raise RuntimeError("session-control store unavailable")
        from hermes_cli.goals import _warn_dropped_write

        _warn_dropped_write("LoopManager", "loop", session_id)
        return None
    key = _meta_key(session_id)

    def write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        stored = _parse_state(row[0]) if row and row[0] else None
        original = stored.to_json() if stored is not None else None
        live = _parse_state(original) if original is not None else None
        result = apply(live) if apply is not None else None
        updated = live if apply is not None else state
        if updated is not None and (apply is None or updated.to_json() != original):
            updated.control_generation = _next_control_generation(
                session_id, stored, updated, tick_lifecycle=tick_lifecycle)
            # Every durable write gets a fresh token so snapshot CAS detects same-value (ABA) changes.
            updated.mutation_id = uuid.uuid4().hex
            db.set_meta(key, updated.to_json(), cursor=conn)
        return updated, result

    # Transaction owners must see failures, not acknowledge an unapplied control.
    saved = write(cursor) if cursor is not None else _db_op(
        "set_meta", lambda: db._execute_write(write))
    if saved is not None:
        _remember_control_generation(session_id, saved[0])
    return saved


def _overlay(live: LoopState, staged: LoopState) -> None:
    """Copy a staged loop onto the live row; ``save_loop`` still assigns the writer-owned tokens."""
    snapshot = LoopState.from_json(staged.to_json())
    for item in fields(LoopState):
        setattr(live, item.name, getattr(snapshot, item.name))


def _tick_outcome_applies(stored: Optional[LoopState], instance: Any, fired_ticks: int,
                          expected_phase: Optional[str] = None, owns_turn=None) -> bool:
    """The one fence for every tick outcome: still this in-flight tick, and no live owner."""
    return bool(
        stored is not None and stored.status == "active" and stored.awaiting_response
        and stored.created_at == instance and stored.ticks_fired == fired_ticks
        and (expected_phase is None or stored.claim_phase == expected_phase)
        and (owns_turn is None or not owns_turn())
    )


def clear_loop(session_id: str) -> None:
    """Retire the current loop without overwriting a concurrent definition or tick."""
    LoopManager(session_id).clear()


def list_active_loops() -> list[tuple[str, LoopState]]:
    """``[(session_id, LoopState), ...]`` for every ACTIVE loop; ``[]`` on any DB error.

    Used by the gateway's idle wakeup watcher, which scans for due loops on a coarse tick.
    """
    db = _get_session_db()
    if db is None:
        return []
    out: list[tuple[str, LoopState]] = []
    for key, raw in _db_op("list_meta_prefix", lambda: db.list_meta_prefix(_META_PREFIX), []):
        session_id = key[len(_META_PREFIX):]
        state = _parse_state(raw) if session_id and raw else None
        if state is not None and state.status == "active":
            out.append((session_id, state))
    return out


def store_has_active_loop(db: Any) -> bool:
    """True when *db* holds an ACTIVE ``loop:*`` row — or a row that cannot be parsed (unknown, so the
    caller keeps its full scan). Unlike :func:`list_active_loops` this takes the store explicitly and
    propagates read errors, so an idle gate can tell "empty" from "unavailable"."""
    for key, raw in db.list_meta_prefix(_META_PREFIX):
        state = _parse_state(raw, key[len(_META_PREFIX):]) if raw else None
        if state is None or state.status == "active":
            return True
    return False


def migrate_loop_to_session(old_session_id: str, new_session_id: str, *, reason: str = "") -> bool:
    """Carry a /loop from a parent session to its continuation. Best-effort, never raises.

    Context compression rotates ``session_id`` to a fresh child; without this the loop silently
    dies at the compaction boundary.

    Copies the loop onto the new session and archives the old row as ``cleared`` so exactly one active loop
    row exists per logical conversation. See #33618.
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        db = _get_session_db()
        if db is None:
            return False
        state = load_loop(old_session_id)
        if state is None or state.status == "cleared":
            return False
        expected = state.to_json()

        def migrate(conn):
            # A control or settlement after discovery wins; never copy stale active state onto
            # the child or clear a newer parent. Both writes share this stored-row transaction.
            current = load_loop(old_session_id, cursor=conn)
            if (current is None or current.to_json() != expected
                    or conn.execute("SELECT value FROM state_meta WHERE key = ?",
                                    (_meta_key(new_session_id),)).fetchone() is not None):
                return False
            save_loop(new_session_id, current, cursor=conn)
            retired = LoopState.from_json(expected)
            retired.status = "cleared"
            retired.awaiting_response = False
            save_loop(old_session_id, retired, cursor=conn)
            return True

        if not db._execute_write(migrate):
            return False
        logger.debug(
            "LoopManager: migrated loop %s -> %s (%s)",
            old_session_id, new_session_id, reason or "rotation",
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("LoopManager: loop migration failed: %s", exc)
        return False


def _ticks_label(n: int) -> str:
    return f"{n} tick{'s' if n != 1 else ''}"


def _dash(reason: Optional[str]) -> str:
    return f" — {reason}" if reason else ""


def response_signals_complete(response: str) -> bool:
    """True when the agent ended its reply with the LOOP_COMPLETE marker."""
    return bool(response) and _LOOP_COMPLETE_RE.search(response) is not None


def _digest_response(response: str) -> str:
    """Digest for self-paced change detection; whitespace-normalized with clock/timestamp/duration
    tokens stripped so 'checked at 14:02:33' doesn't defeat the backoff."""
    text = (response or "").strip().lower()
    if _is_silence_reply(response):
        # Every silence marker form (NO_REPLY, "[SILENT].") is the same "nothing changed" reply,
        # matching the --until judge skip, so repeated quiet ticks back off.
        return SILENCE_MARKER.lower()
    text = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", "", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    text = re.sub(r"\b\d+(\.\d+)?\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)\b", "", text)
    text = re.sub(r"\s+", " ", text)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class LoopManager(LoopRevisionsMixin):
    """Per-session /loop state + tick decisions.

    Drivers call ``set``/``pause``/``resume``/``clear`` for user controls, ``is_due()`` (cheap,
    in-memory), ``fire_tick()`` to claim a tick and get the wakeup message, ``complete_tick()`` to
    evaluate the finished turn, and ``status_line()``.
    """

    def __init__(self, session_id: str, *, cursor=None):
        self._snapshot_lock = threading.RLock()
        self.session_id = session_id
        self._cursor = cursor
        self._state: Optional[LoopState] = (
            load_loop(session_id) if cursor is None else load_loop(session_id, cursor=cursor)
        )
        self._mark_synced()

    def _mark_synced(self) -> None:
        """Remember the stored schedule this manager last read or wrote.

        A tick claim compares it with the stored row: a different value means another writer
        (pause/resume, revise, cadence change) rescheduled the loop since, and that schedule wins
        over this manager's cached copy."""
        self._synced_due_at = self._state.next_due_at if self._state is not None else None
        self._synced_json = self._state.to_json() if self._state is not None else None

    @property
    def state(self) -> Optional[LoopState]:
        return self._state

    @snapshot_control
    def refresh(self) -> None:
        """Re-read state from the DB (cross-process safety for the gateway).

        A failed read also yields None; keep the cached state then, or a transient DB error during a
        wakeup would drop ``awaiting_response`` handling and wedge the tick. Rows are never deleted
        (stop marks them cleared), so None with a cached state means the read failed."""
        fresh = self._load()
        if fresh is not None or self._state is None:
            self._state = fresh
            self._mark_synced()

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_loop(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def _load(self) -> Optional[LoopState]:
        # A cursor owner holds the write lock; reading outside it would block on that same lock.
        return (load_loop(self.session_id) if self._cursor is None
                else load_loop(self.session_id, cursor=self._cursor))

    def _mutate_current(self, apply, *, failure=None, tick_lifecycle: bool = False) -> Any:
        """Run one command against the stored row, then publish only the durable winner.

        ``apply`` validates and edits the live row inside the write transaction and returns the
        command result; it may be replayed on lock contention. The shared snapshot boundary then
        adopts the committed row unless a newer same-manager command already published, so a
        rejected command still refreshes this manager. A failed write publishes nothing and
        returns ``failure``; cursor owners see the error instead.
        """
        from hermes_cli.loops_snapshot import LoopPersistenceError

        if not self.session_id:
            return apply(self._state)  # intentionally ephemeral: there is no stored row
        written = []

        def commit() -> Optional[LoopState]:
            saved = save_loop(self.session_id, cursor=self._cursor, apply=apply,
                              tick_lifecycle=tick_lifecycle)
            if saved is None:
                raise LoopPersistenceError("loop write failed")
            written.append(saved)
            return saved[0]

        try:
            # Rows are never deleted, so a failed winner read keeps the committed row.
            commit_and_publish_snapshot(self, None, commit, lambda: self._load() or written[-1][0])
        except LoopPersistenceError:
            return failure
        self._mark_synced()
        return written[-1][1]

    def _save(self) -> Optional[LoopState]:
        """Legacy snapshot edits are CAS-only; commands use ``_mutate_current``."""
        snapshot = getattr(self, "_evaluation_snapshot", None)
        if snapshot is not None:
            # Staged settlement/revision: the snapshot CAS commits, never this direct write.
            from hermes_cli.loops_snapshot import assert_loop_snapshot
            db, expected = snapshot
            assert_loop_snapshot(self.session_id, expected, db, cursor=self._cursor)
            return self._state
        candidate = self._state

        def apply(stored):
            if stored is not None and stored.to_json() == self._synced_json and candidate is not None:
                _overlay(stored, candidate)

        self._mutate_current(apply)
        return self._state

    def status_line(self) -> str:
        s = self._state
        if s is None or s.status == "cleared":
            return "No loop set. Start one with /loop [interval] <prompt>."
        fired = _ticks_label(s.ticks_fired)
        if s.times:
            caps = [f"{s.ticks_fired}/{s.times} runs"]
        elif s.max_ticks:
            caps = [f"{s.ticks_fired}/{s.max_ticks} budget"]
        else:
            caps = [fired]
        if s.until:
            caps.append(f"until: {s.until}")
        meta = f"{s.cadence_label()}, {', '.join(caps)}"
        version = f"v{s.version}, " if s.revisions else ""
        if s.status == "active":
            remaining = s.remaining_label()
            tail = ", wakeup running" if s.awaiting_response else (f", {remaining}" if remaining else "")
            return f"↻ Loop (active, {version}{meta}{tail}): {s.prompt}"
        if s.status == "paused":
            return f"⏸ Loop (paused, {version}{meta}{_dash(s.paused_reason)}): {s.prompt}"
        if s.status == "done":
            return f"✓ Loop finished ({version}{fired}{_dash(s.last_stop_reason)}): {s.prompt}"
        return f"Loop ({s.status}, {version}{meta}): {s.prompt}"

    @snapshot_control
    def set(
        self,
        prompt: str,
        *,
        interval_seconds: Optional[int] = None,
        times: int = 0,
        until: str = "",
        route: Optional[dict[str, str]] = None,
    ) -> LoopState:
        """Start a new loop (replaces any existing one for the session)."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("loop prompt is empty")

        # created_at is the loop-instance fence for tick claims and outcomes, so a new instance
        # must be strictly later than the stored one (cleared rows stay stored) even when the
        # clock is coarse or frozen; otherwise a stale scheduler or completion could match it.
        stored = load_loop(self.session_id) if self._cursor is None else load_loop(
            self.session_id, cursor=self._cursor)
        prior = max(
            float(getattr(self._state, "created_at", 0.0) or 0.0),
            float(getattr(stored, "created_at", 0.0) or 0.0),
        )
        now = max(time.time(), prior + 1e-6)
        self_paced = interval_seconds is None
        interval = 0.0 if self_paced else float(max(int(interval_seconds), min_interval_seconds()))
        state = LoopState(
            prompt=prompt,
            mode="self_paced" if self_paced else "interval",
            interval_seconds=interval,
            current_delay=float(self_paced_floor_seconds()) if self_paced else interval,
            times=max(0, int(times or 0)),
            until=(until or "").strip(),
            max_ticks=max_ticks_default(),
            created_at=now,
            next_due_at=now,
            route=dict(route or {}),
            goal_warning=_active_goal_loop_warning(self.session_id) or None,
        )
        if not self.session_id:
            self._state = state  # intentionally ephemeral
            return state

        def commit() -> LoopState:
            saved = save_loop(self.session_id, state, cursor=self._cursor)
            if saved is None:
                raise ValueError("loop could not be saved")
            return saved[0]

        commit_and_publish_snapshot(self, None, commit, lambda: self._load() or state)
        self._mark_synced()
        return self._state

    @snapshot_control
    def pause(self, reason: str = "user-paused") -> Optional[LoopState]:
        def apply(state):
            if state is None or state.status not in {"active", "paused"}:
                return None
            state.status, state.paused_reason, state.awaiting_response = "paused", reason, False
            return state

        return self._mutate_current(apply)

    @snapshot_control
    def resume(self) -> Optional[LoopState]:
        def apply(state):
            if state is None or state.status == "cleared":
                return None
            state.status, state.paused_reason, state.awaiting_response = "active", None, False
            state.backoff_level = 0
            state.last_response_digest = ""
            if state.mode == "self_paced":
                state.current_delay = float(self_paced_floor_seconds())
            # Re-arm relative to now so a long pause doesn't fire instantly N times.
            delay = state.current_delay or state.interval_seconds or self_paced_floor_seconds()
            state.next_due_at = time.time() + min(delay, 5.0)
            return state

        return self._mutate_current(apply)

    @snapshot_control
    def wake_now_for_external_event(self) -> bool:
        """Make one idle loop tick due immediately for trusted outside evidence.

        The stored-row mutation is a no-op while a tick is already in flight, so repeated delivery
        signals cannot create duplicate ticks.
        """
        def apply(state):
            if state is None or state.status != "active" or state.awaiting_response:
                return False
            now = time.time()
            minimum_gap = state.interval_seconds or self_paced_floor_seconds()
            if (
                state.next_due_at <= now
                or state.ticks_fired <= state.last_external_wake_tick
                or (state.last_fired_at and now - state.last_fired_at < minimum_gap)
            ):
                return False
            state.next_due_at = now
            state.last_external_wake_tick = state.ticks_fired
            state.backoff_level = 0
            state.last_response_digest = ""
            if state.mode == "self_paced":
                state.current_delay = float(self_paced_floor_seconds())
            else:
                state.current_delay = state.interval_seconds
            return True

        return bool(self._mutate_current(apply, failure=False))

    @snapshot_control
    def clear(self) -> bool:
        def apply(state):
            if state is None or state.status == "cleared":
                return False
            state.status = "cleared"
            state.awaiting_response = False
            return True

        cleared = self._mutate_current(apply, failure=False)
        if cleared:
            self._state = None
            self._mark_synced()
        return bool(cleared)

    def is_due(self, now: Optional[float] = None) -> bool:
        """Cheap check: active, not mid-wakeup, and the clock has passed."""
        s = self._state
        return (
            s is not None and s.status == "active" and not s.awaiting_response
            and (now if now is not None else time.time()) >= s.next_due_at
        )

    @snapshot_control
    def fire_tick(self, run_generation: int = 0, *, can_claim=None) -> Optional[str]:
        """Atomically claim and start a due tick before any wake preparation or effects.

        Admission consumes the tick even if delivery or execution is interrupted. The stored
        row owns the exact run token. Failed admission runs no work and needs no refund.
        """
        s = self._state
        if s is None or not self.is_due():
            return None

        def claim(state: LoopState) -> None:
            state.ticks_fired += 1
            state.tick_id = uuid.uuid4().hex  # survives revisions, changes on every claim
            state.last_fired_at = time.time()
            state.awaiting_response = True
            state.claim_phase = "started"
            state.run_generation = int(run_generation)
            state.fired_version = state.version
            # Provisional schedule from NOW: complete_tick reschedules from turn end, but if the
            # process dies mid-turn this keeps the persisted loop from being 'due' in a tight loop.
            state.next_due_at = state.last_fired_at + (
                state.current_delay or state.interval_seconds or self_paced_floor_seconds())

        if not self._claim_tick(claim, can_claim=can_claim):
            return None
        s = self._state

        if s.prompt.lstrip().startswith("/"):
            return s.prompt.strip()
        cadence = f", {s.cadence_label()}" if s.mode == "interval" else ", self-paced"
        template = WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE if s.until else WAKEUP_PROMPT_TEMPLATE
        return template.format(tick=s.ticks_fired, cadence=cadence, prompt=s.prompt, until=s.until)

    def _claim_tick(self, claim, *, can_claim=None) -> bool:
        """Claim the due tick on the stored loop inside one write transaction.

        A scheduler loads its manager before awaiting the executor that fires the tick, so a pause,
        stop, revise or re-set can commit in between; saving the cached copy would resurrect it. The
        claim applies only while the stored row is still this loop instance, active, idle and at the
        cached control generation (compare-and-swap): a control another manager committed since this
        one loaded rejects the claim and refreshes the cache, never writing the stale fence back. A
        revise in between fires the stored (revised) definition; otherwise the caller's copy fires."""
        s = self._state
        # An unbound manager is intentionally ephemeral: there is no stored row to fence against.
        db = _get_session_db() if self._cursor is None and self.session_id else None
        if db is None and self._cursor is None:
            if can_claim is not None and not can_claim():
                return False
            claim(s)
            save_loop(self.session_id, s, tick_lifecycle=True)
            self._mark_synced()
            return True

        def apply(stored: Optional[LoopState]) -> bool:
            if (
                stored is None or stored.status != "active" or stored.awaiting_response
                or (can_claim is not None and not can_claim())
                or stored.created_at != s.created_at or stored.ticks_fired != s.ticks_fired
                or stored.control_generation != s.control_generation
            ):
                return False
            # Another writer's schedule since this manager's last sync governs: a revise,
            # pause/resume or cadence change can postpone the tick. A revise since load fires the
            # stored definition.
            rescheduled = stored.next_due_at != self._synced_due_at or stored.version != s.version
            if rescheduled and time.time() < stored.next_due_at:
                return False
            claimed = _parse_state((s if stored.version == s.version else stored).to_json())
            claim(claimed)
            _overlay(stored, claimed)
            return True

        # A cursor owner already holds the write transaction: the same guarded stored-row check
        # runs through it, and its failures propagate as save_loop(cursor=...) does.
        return bool(self._mutate_current(apply, failure=False, tick_lifecycle=True))

    def _save_tick_outcome(self, fired_ticks: int, settle, *, expected_phase=None, owns_turn=None) -> bool:
        """Settle the fired tick on the stored loop inside one write transaction.

        A user or cross-session pause, stop, resume, replace or re-set can land while the tick's
        turn runs; the in-memory copy predates it, and saving that copy would resurrect a stopped
        loop or undo a pause. So the outcome applies only while the stored row is still this
        in-flight tick (active, awaiting_response, same created_at and ticks_fired). A revise keeps
        the tick in flight by design, so ``settle`` runs against the stored row and the revised
        definition survives while the tick still clears awaiting_response."""
        s = self._state
        if getattr(self, "_evaluation_snapshot", None) is not None:
            # Staged by mutate_loop_snapshot: the snapshot CAS is the conditional write, and the
            # staged copy is the snapshot it validates, so the same outcome fence applies to it.
            if not _tick_outcome_applies(s, getattr(s, "created_at", None), fired_ticks,
                                         expected_phase, owns_turn):
                return False
            settle(s)
            return True
        db = _get_session_db() if self._cursor is None and self.session_id else None
        if s is None:
            return False
        if db is None and self._cursor is None:
            settle(s)
            save_loop(self.session_id, s, tick_lifecycle=True)
            self._mark_synced()
            return True

        instance = s.created_at

        def apply(stored: Optional[LoopState]) -> bool:
            if not _tick_outcome_applies(stored, instance, fired_ticks, expected_phase, owns_turn):
                return False
            settle(stored)
            return True

        if self._mutate_current(apply, failure=False, tick_lifecycle=True):
            return True
        # Compression migrated the loop (verbatim, same instance and tick) after this manager
        # loaded it; the claim now lives on the continuation. Settle it there, under the same
        # conditional fence, so an abandoned or completed tick never strands the child at
        # awaiting_response. A user pause or stop on the child still rejects the outcome.
        return self._follow_compression() and bool(
            self._mutate_current(apply, failure=False, tick_lifecycle=True))

    def _follow_compression(self) -> bool:
        """Rebind this manager to a verified compression continuation holding the same loop."""
        s = self._state
        if s is None or self._cursor is not None:
            return False
        from hermes_cli.session_controls import migrated_successor

        successor = migrated_successor("loop", self.session_id)
        moved = load_loop(successor) if successor else None
        if moved is None or moved.created_at != s.created_at:
            return False
        logger.debug("LoopManager: following migrated loop %s -> %s", self.session_id, successor)
        self.session_id = successor
        return True

    def _superseded_result(self) -> Dict[str, Any]:
        s = self._state
        return {"status": s.status if s else None, "stopped": False,
                "reason": "loop changed during the tick", "message": ""}

    @snapshot_control
    def abandon_tick(self, *, tick: Optional[Tuple[Any, Any]] = None, owns_turn=None) -> bool:
        """Release a pre-start claim, or skip a started tick without replaying it.

        ``fire_tick`` normally claims and starts in one CAS, so admission failures after that
        boundary must clear ``awaiting_response`` while retaining ``ticks_fired``. The legacy
        ``claimed`` phase remains refundable for recovery of rows written before the atomic CAS.
        """
        s = self._state
        if s is None or not s.awaiting_response:
            return False
        if tick is not None:
            try:
                instance, fired = tick
            except (TypeError, ValueError):
                return False
            if s.created_at != instance or s.ticks_fired != fired:
                if not self._follow_compression():
                    return False
                s = self._state
                if s is None or not s.awaiting_response:
                    return False
                if s.created_at != instance or s.ticks_fired != fired:
                    return False
        fired = s.ticks_fired
        phase = s.claim_phase
        if phase not in {"claimed", "started", ""}:
            return False

        def settle(state: LoopState) -> None:
            state.awaiting_response = False
            if phase == "claimed":
                state.ticks_fired = max(0, fired - 1)  # legacy pre-start claim: nothing ran
            # Deterministic caps also govern a released tick (a revise may have lowered them).
            cap = self._cap_stop(state)
            if cap is not None:
                self._apply_stop(state, cap[0], cap[1])
            elif phase == "claimed":
                state.next_due_at = time.time()
            else:
                self._settle_continue(state, "", time.time())

        return self._save_tick_outcome(fired, settle, expected_phase=phase, owns_turn=owns_turn)

    def _tick_execution_current(self, tick: Tuple[Any, Any], *, control_generation: int,
                                run_generation: int) -> bool:
        """Read the stored admission fence off-loop before each effect producer."""
        s = load_loop(self.session_id)
        return bool(
            s is not None and s.status == "active" and s.awaiting_response
            and (s.created_at, s.ticks_fired) == tick and s.claim_phase == "started"
            and s.control_generation == int(control_generation)
            and s.run_generation == int(run_generation)
        )

    def recover_tick(self, *, owns_turn=None) -> str:
        """Skip an ownerless interrupted tick and schedule its successor, never replay it."""
        s = self._state
        if s is None or not s.awaiting_response:
            return ""
        reason = "interrupted loop tick skipped: execution outcome unknown"

        def skip(state):
            cap = self._cap_stop(state)
            if cap is not None:
                self._apply_stop(state, cap[0], cap[1])
            else:
                self._settle_continue(state, "", time.time())

        return reason if self._save_tick_outcome(
            s.ticks_fired, skip, expected_phase=s.claim_phase, owns_turn=owns_turn,
        ) else ""

    @staticmethod
    def _cap_stop(state: LoopState) -> Optional[tuple[str, str, str]]:
        """The run-count or backstop cap the stored definition has reached, if any.

        Caps count ticks, not response content, so a revise cannot excuse them: they are always
        judged against the current stored definition, never the fired version."""
        if state.times and state.ticks_fired >= state.times:
            return ("done", f"completed the requested {state.times} runs",
                    f"✓ Loop finished — ran {state.times}/{state.times} times.")
        if state.max_ticks and state.ticks_fired >= state.max_ticks:
            return ("paused", f"tick budget exhausted ({state.ticks_fired}/{state.max_ticks})",
                    f"⏸ Loop paused — {state.ticks_fired}/{state.max_ticks} ticks used "
                    "(loops.max_ticks). /loop resume to keep going, /loop stop to end it.")
        return None

    def _stop(self, status: str, reason: str, message: str, *, judged_version: int,
              digest: str = "") -> dict[str, Any]:
        """Persist a terminal (``done``) or recoverable (``paused``) stop and build the result.

        The verdict was reached against definition ``judged_version``. If a revise changed the
        definition while the tick was being evaluated, the verdict no longer describes the loop:
        settle the tick as a normal continue instead, and the next tick judges the new definition.
        """
        s = self._state
        superseded = False

        cap: Optional[Tuple[str, str, str]] = None

        def settle(state: LoopState) -> None:
            nonlocal superseded, cap
            if state.version != judged_version:
                superseded = True
                cap = self._cap_stop(state)
                if cap is not None:
                    self._apply_stop(state, cap[0], cap[1])
                    return
                self._settle_continue(state, digest, time.time())
                return
            state.awaiting_response = False
            state.status = status
            if status == "done":
                state.last_stop_reason = reason
            else:
                state.paused_reason = reason

        if s is None or not self._save_tick_outcome(s.ticks_fired, settle):
            return self._superseded_result()
        if cap is not None:
            return {"status": cap[0], "stopped": True, "reason": cap[1], "message": cap[2]}
        if superseded:
            return {"status": "active", "stopped": False,
                    "reason": "loop revised during evaluation", "message": ""}
        return {"status": status, "stopped": True, "reason": reason, "message": message}

    @staticmethod
    def _apply_stop(state: LoopState, status: str, reason: str) -> None:
        state.awaiting_response = False
        state.status = status
        if status == "done":
            state.last_stop_reason = reason
        else:
            state.paused_reason = reason

    @staticmethod
    def _settle_continue(state: LoopState, digest: str, now: float) -> None:
        """Clear the in-flight tick and schedule the next one on the stored cadence."""
        state.awaiting_response = False
        if state.mode == "self_paced":
            floor = self_paced_floor_seconds()
            if digest and digest == state.last_response_digest:
                level = min(max(0, state.backoff_level), len(_NO_PROGRESS_BACKOFF_S) - 1)
                state.current_delay = float(min(
                    max(floor, _NO_PROGRESS_BACKOFF_S[level]),
                    self_paced_ceiling_seconds(),
                ))
                state.backoff_level = min(level + 1, len(_NO_PROGRESS_BACKOFF_S) - 1)
            else:
                state.current_delay = float(floor)
                state.backoff_level = 0
            state.last_response_digest = digest
        else:
            state.current_delay = state.interval_seconds
            state.backoff_level = 0
        state.next_due_at = now + state.current_delay

    @staticmethod
    def _settle_blocked_until(state: LoopState, now: float) -> None:
        """Keep an unachievable --until loop active, with one step of quiet backoff."""
        state.awaiting_response = False
        level = min(max(0, state.backoff_level), len(_NO_PROGRESS_BACKOFF_S) - 1)
        delay = _NO_PROGRESS_BACKOFF_S[level]
        if state.mode == "self_paced":
            delay = min(max(self_paced_floor_seconds(), delay), self_paced_ceiling_seconds())
            state.current_delay = float(delay)
        else:
            delay = max(delay, state.interval_seconds)
            state.current_delay = state.interval_seconds
        state.backoff_level = min(level + 1, len(_NO_PROGRESS_BACKOFF_S) - 1)
        state.next_due_at = now + delay

    def complete_tick(self, last_response: str, *, tick: Optional[Tuple[Any, Any]] = None) -> Dict[str, Any]:
        """Settle the finished wakeup turn through the durable snapshot/CAS boundary.

        A superseded verdict cannot overwrite a newer durable state; see ``_complete_tick``.
        """
        from hermes_cli.loops_snapshot import mutate_loop_snapshot
        return mutate_loop_snapshot(self, "_complete_tick", last_response, tick=tick)

    def _complete_tick(self, last_response: str, *, tick: Optional[tuple[Any, Any]] = None) -> dict[str, Any]:
        """Evaluate the finished wakeup turn and schedule what's next.

        ``tick`` is the ``(loop instance, ticks_fired)`` the completed turn's wake carried. A fresh
        manager would otherwise pin whatever tick is in flight when it loads, which after a stop
        and re-set is a newer tick this response never answered.

        Returns ``{"status": "active|done|paused", "stopped": bool, "reason": str, "message": str}``;
        ``message`` is a user-visible one-liner, "" in the common still-looping case.
        """
        # The response answers the tick its wake carried, else the tick this manager fired (or,
        # for a fresh manager, the tick in flight when it loaded): pin that tick's instance and
        # count before reading the stored loop, so a revise, re-set or newer tick never inherits
        # its verdict.
        fired = self._state
        if fired is None or not fired.awaiting_response:
            return {"status": fired.status if fired else None, "stopped": False,
                    "reason": "no tick in flight", "message": ""}
        fired_instance, fired_tick = tick if tick is not None else (fired.created_at, fired.ticks_fired)
        self.refresh()
        s = self._state
        if (
            s is None or not s.awaiting_response or s.created_at != fired_instance
            or s.ticks_fired != fired_tick
        ):
            return self._superseded_result()
        now = time.time()
        ticks = _ticks_label(s.ticks_fired)
        # Verdicts judge the fired definition; a revise since the fire supersedes stop verdicts.
        # Ticks claimed before fired_version existed fall back to the loaded copy's version.
        judged = {"judged_version": s.fired_version or fired.version,
                  "digest": _digest_response(last_response)}

        # 1. Agent self-stop marker.
        if response_signals_complete(last_response):
            return self._stop("done", "agent signaled the task is complete",
                              f"✓ Loop finished after {ticks} — task complete.", **judged)

        # 2. Evidence-based --until judge (reuses the /goal judge; fail-open). A bare silence
        # marker is the prompt's "nothing changed" reply: no evidence to judge, so no model call.
        if s.until and (last_response or "").strip() and not _is_silence_reply(last_response):
            try:
                from hermes_cli.goals import judge_goal

                verdict, reason, _pf, _wait, _tf = judge_goal(s.until, last_response)
            except Exception as exc:
                logger.warning("Loop stop-condition judge unavailable", exc_info=True)
                verdict, reason = "continue", f"judge unavailable: {type(exc).__name__}"
            if verdict == "done":
                return self._stop("done", f"stop condition met: {reason}",
                                  f"✓ Loop finished after {ticks} — {reason}", **judged)
            if verdict == "blocked":
                # An unachievable stop condition is no-progress evidence, not a terminal loop
                # failure. Keep the loop active and use the shared quiet ladder before retrying.
                why = f"stop condition judged unachievable: {reason}"
                cap: Optional[Tuple[str, str, str]] = None

                def settle_blocked(state: LoopState) -> None:
                    nonlocal cap
                    self._settle_blocked_until(state, now)
                    cap = self._cap_stop(state)
                    if cap is not None:
                        self._apply_stop(state, cap[0], cap[1])

                if not self._save_tick_outcome(s.ticks_fired, settle_blocked):
                    return self._superseded_result()
                if cap is not None:
                    return {"status": cap[0], "stopped": True, "reason": cap[1], "message": cap[2]}
                return {
                    "status": "active", "stopped": False, "reason": why,
                    "message": f"⏳ Loop still active — {why}; next check delayed.",
                }

        # 3-4. --times user cap (done) and config backstop budget (paused, recoverable), judged at
        # settle time against the stored definition so a revise mid-tick cannot lift either.
        cap: Optional[Tuple[str, str, str]] = None

        def settle(state: LoopState) -> None:
            nonlocal cap
            cap = self._cap_stop(state)
            if cap is not None:
                self._apply_stop(state, cap[0], cap[1])
                return
            # 5. Still looping — schedule the next tick from turn end, using the stored cadence so
            # a revise that landed during this tick takes effect now.
            self._settle_continue(state, judged["digest"], now)

        if not self._save_tick_outcome(s.ticks_fired, settle):
            return self._superseded_result()
        if cap is not None:
            return {"status": cap[0], "stopped": True, "reason": cap[1], "message": cap[2]}
        return {"status": "active", "stopped": False, "reason": "loop continues", "message": ""}


def enforce_loop_run_cap(state: LoopState) -> Optional[Dict[str, Any]]:
    """Apply deterministic budgets without judging an obsolete or absent reply."""
    if state.status != "active":
        return None
    cap = LoopManager._cap_stop(state)
    if cap is None:
        return None
    LoopManager._apply_stop(state, cap[0], cap[1])
    return {"status": cap[0], "stopped": True, "reason": cap[1], "message": cap[2]}


def wake_loop_for_external_event(session_id: str) -> bool:
    """Pierce an active loop's delay for one admitted trusted external event."""
    try:
        return LoopManager(session_id=session_id).wake_now_for_external_event()
    except Exception:
        return False


def _active_goal_loop_warning(session_id: Optional[str]) -> str:
    """Warn only when the scheduler's goal predicate currently blocks loop ticks."""
    if not session_id or not goal_blocks_loop_tick(session_id):
        return ""
    return (
        "⚠ An active goal is driving this session: loop ticks defer now, "
        "only while the goal is actively taking turns. "
        "A goal that is parked/waiting, paused, or done does not block loop ticks. "
        "Keep this as a separate recurring duty only if that deferral is intended."
    )


def goal_blocks_loop_tick(session_id: str) -> bool:
    """True when an ACTIVE, non-parked /goal should defer this session's /loop tick.

    Both features inject synthetic turns at idle boundaries; interleaving them would burn the
    goal's turn budget. Parked (waiting), paused, or done goals do NOT block the loop.
    """
    try:
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=session_id)
        return mgr.is_active() and not mgr.is_waiting()
    except Exception:
        return False


LOOP_HELP = (
    "Usage: /loop [interval] <prompt> [--times N] [--until <condition>]\n"
    "  /loop 5m check the deploy status      — first run now, then every 5m\n"
    "  /loop every 10m /recap                — loop a slash command\n"
    "  /loop keep fixing tests until green   — self-paced (backs off while output is unchanged)\n"
    "  /loop 2m poll CI --times 30           — stop after 30 runs\n"
    "  /loop 5m watch the queue --until queue is empty\n"
    "Controls: /loop status · /loop pause · /loop resume · /loop stop\n"
    "The loop also stops itself when the agent replies with "
    f"{LOOP_COMPLETE_MARKER}."
)


def _pause_output(mgr: LoopManager) -> str:
    state = mgr.pause(reason="user-paused")
    return "No loop set." if state is None else f"⏸ Loop paused: {state.prompt}\nUse /loop resume to continue."


def _resume_output(mgr: LoopManager) -> str:
    state = mgr.resume()
    return "No loop to resume." if state is None else f"▶ Loop resumed ({state.cadence_label()}): {state.prompt}"


# Control words -> handler returning the output text. Anything else is a new loop spec.
_CONTROL_COMMANDS = {
    **dict.fromkeys(("", "status"), lambda mgr: mgr.status_line()),
    "pause": _pause_output,
    "resume": _resume_output,
    **dict.fromkeys(("stop", "clear", "cancel"), lambda mgr: "✓ Loop stopped." if mgr.clear() else "No active loop."),
    **dict.fromkeys(("help", "--help", "-h"), lambda mgr: LOOP_HELP),
}


def dispatch_loop_command(
    mgr: LoopManager,
    args: str,
    *,
    route: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Surface-agnostic handler for ``/loop <args>`` → ``{"output": str, "created": bool}``.

    ``output`` is printed/sent verbatim by each surface. ``route`` is stored on new loops so the
    gateway's idle watcher can inject wakeups into the right chat; CLI/TUI pass None.
    """
    arg = (args or "").strip()
    control = _CONTROL_COMMANDS.get(arg.lower())
    if control is not None:
        return {"output": control(mgr), "created": False}

    parsed = parse_loop_args(arg)
    if parsed["error"]:
        if parsed["error"] == "empty":
            return {"output": "Usage: /loop [interval] <prompt> — see /loop help.", "created": False}
        return {"output": f"/loop: {parsed['error']}", "created": False}

    replacing = mgr.has_loop()
    try:
        state = mgr.set(
            parsed["prompt"],
            interval_seconds=parsed["interval_seconds"],
            times=parsed["times"],
            until=parsed["until"],
            route=route,
        )
    except ValueError as exc:
        return {"output": f"/loop: {exc}", "created": False}

    lines = [f"↻ Loop set ({state.cadence_label()}): {state.prompt}"]
    if replacing:
        lines.append("(replaced the previous loop for this session)")
    overlap_warning = _active_goal_loop_warning(mgr.session_id)
    if overlap_warning:
        lines.append(overlap_warning)
    if parsed["interval_seconds"] is not None and parsed["interval_seconds"] < state.interval_seconds:
        lines.append(
            f"(interval raised to the {format_interval(state.interval_seconds)} minimum — "
            "loops.min_interval_seconds)"
        )
    if state.mode == "self_paced":
        lines.append(
            f"Self-paced: first check in {format_interval(state.current_delay)}; "
            f"backs off up to {format_interval(self_paced_ceiling_seconds())} while nothing changes."
        )
    if state.times:
        lines.append(f"Runs {state.times} time{'s' if state.times != 1 else ''}, then stops.")
    if state.until:
        lines.append(f"Stops when: {state.until}")
    if not state.times and state.max_ticks:
        lines.append(f"Backstop budget: {state.max_ticks} ticks (loops.max_ticks; 0 = unlimited).")
    first = "fires now, then on the cadence above" if state.status == "active" else state.remaining_label()
    lines.append(f"First wakeup {first}. Controls: /loop status · pause · resume · stop.")
    return {"output": "\n".join(lines), "created": True}


__all__ = [
    "LoopState", "LoopManager", "parse_loop_args", "parse_interval_token", "format_interval",
    "response_signals_complete", "goal_blocks_loop_tick", "wake_loop_for_external_event", "load_loop", "save_loop", "clear_loop",
    "list_active_loops", "migrate_loop_to_session", "dispatch_loop_command", "loop_control_generation",
    "LOOP_COMPLETE_MARKER",
    "WAKEUP_PROMPT_TEMPLATE", "WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE", "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEFAULT_MAX_TICKS", "is_quiet_wakeup_prompt",
]
