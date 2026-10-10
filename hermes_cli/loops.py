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
import time
from dataclasses import dataclass, field, fields, asdict
from typing import Any, Dict, List, Optional, Tuple

from hermes_cli.heartbeat import HEARTBEAT_PROMPT_PREFIX, SILENCE_MARKER, is_intentional_silence_response
from hermes_cli.goals import _AUTONOMOUS_REPLY_RULES

logger = logging.getLogger(__name__)


# Floor for fixed intervals. Claude Code allows 30s; anything tighter is almost always an
# accident that burns tokens polling unchanged state. Config loops.min_interval_seconds (clamped ≥ 5).
DEFAULT_MIN_INTERVAL_SECONDS = 30
# Backstop tick budget so an unattended loop can't run forever. 0 = unlimited; config loops.max_ticks.
DEFAULT_MAX_TICKS = 100
# Self-paced mode: start at the floor, double while replies are unchanged, cap at the
# ceiling, snap back to the floor on any change.
DEFAULT_SELF_PACED_FLOOR_SECONDS = 60
DEFAULT_SELF_PACED_CEILING_SECONDS = 15 * 60

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


def parse_loop_args(text: str) -> Dict[str, Any]:
    """Parse ``/loop [interval] <prompt> [--times N] [--until ...]``.

    Returns ``{"interval_seconds": int|None, "prompt", "times", "until", "error"}``;
    ``interval_seconds`` None means self-paced, ``error`` is set for unusable input.
    """
    raw = (text or "").strip()
    result: Dict[str, Any] = {"interval_seconds": None, "prompt": "", "times": 0, "until": "", "error": None}
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


def _loops_config() -> Dict[str, Any]:
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
    last_fired_at: float = 0.0
    next_due_at: float = 0.0
    # True between "wakeup injected" and "that turn's response evaluated": stops a tick from
    # double-firing mid-turn and tells the post-turn hook the turn that just ended was ours.
    awaiting_response: bool = False
    last_response_digest: str = ""    # self-paced change detection
    paused_reason: Optional[str] = None
    last_stop_reason: Optional[str] = None
    # Gateway routing (platform / chat_id / chat_type / thread_id) captured at creation so the
    # idle watcher can inject ticks into the right chat. Empty for CLI/TUI (own schedulers).
    route: Dict[str, str] = field(default_factory=dict)
    # Versioned agent-authored changes; old rows omit this field and load as an empty list.
    revisions: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def version(self) -> int:
        return len(self.revisions) + 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "LoopState":
        data = json.loads(raw)
        route = data.get("route")
        revisions = data.get("revisions", [])
        kwargs: Dict[str, Any] = {
            "prompt": data.get("prompt", ""),
            "status": data.get("status", "active"),
            "mode": data.get("mode", "interval"),
            "paused_reason": data.get("paused_reason"),
            "last_stop_reason": data.get("last_stop_reason"),
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


def _meta_key(session_id: str) -> str:
    return f"{_META_PREFIX}{session_id}"


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
    return _parse_state(raw, session_id) if raw else None


def save_loop(session_id: str, state: LoopState, *, cursor=None) -> None:
    """Persist a loop to SessionDB. No-op if DB unavailable."""
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        if cursor is not None:
            raise RuntimeError("session-control store unavailable")
        from hermes_cli.goals import _warn_dropped_write

        _warn_dropped_write("LoopManager", "loop", session_id)
        return
    if cursor is not None:
        # Transaction owners must see failures, not report an unapplied control as successful.
        db.set_meta(_meta_key(session_id), state.to_json(), cursor=cursor)
    else:
        _db_op("set_meta", lambda: db.set_meta(_meta_key(session_id), state.to_json()))


def clear_loop(session_id: str) -> None:
    """Mark a loop cleared in the DB (preserved for audit, status=cleared)."""
    state = load_loop(session_id)
    if state is not None:
        state.status = "cleared"
        save_loop(session_id, state)


def list_active_loops() -> List[Tuple[str, LoopState]]:
    """``[(session_id, LoopState), ...]`` for every ACTIVE loop; ``[]`` on any DB error.

    Used by the gateway's idle wakeup watcher, which scans for due loops on a coarse tick.
    """
    db = _get_session_db()
    if db is None:
        return []
    out: List[Tuple[str, LoopState]] = []
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
        state = load_loop(old_session_id)
        if state is None or state.status == "cleared" or load_loop(new_session_id) is not None:
            return False
        save_loop(new_session_id, state)
        clear_loop(old_session_id)
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


class LoopManager:
    """Per-session /loop state + tick decisions.

    Drivers call ``set``/``pause``/``resume``/``clear`` for user controls, ``is_due()`` (cheap,
    in-memory), ``fire_tick()`` to claim a tick and get the wakeup message, ``complete_tick()`` to
    evaluate the finished turn, and ``status_line()``.
    """

    def __init__(self, session_id: str, *, cursor=None):
        self.session_id = session_id
        self._cursor = cursor
        self._state: Optional[LoopState] = (
            load_loop(session_id) if cursor is None else load_loop(session_id, cursor=cursor)
        )

    @property
    def state(self) -> Optional[LoopState]:
        return self._state

    def refresh(self) -> None:
        """Re-read state from the DB (cross-process safety for the gateway).

        A failed read also yields None; keep the cached state then, or a transient DB error during a
        wakeup would drop ``awaiting_response`` handling and wedge the tick. Rows are never deleted
        (stop marks them cleared), so None with a cached state means the read failed."""
        fresh = load_loop(self.session_id)
        if fresh is not None or self._state is None:
            self._state = fresh

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_loop(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def _save(self) -> LoopState:
        if self._cursor is None:
            save_loop(self.session_id, self._state)
        else:
            save_loop(self.session_id, self._state, cursor=self._cursor)
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

    def set(
        self,
        prompt: str,
        *,
        interval_seconds: Optional[int] = None,
        times: int = 0,
        until: str = "",
        route: Optional[Dict[str, str]] = None,
    ) -> LoopState:
        """Start a new loop (replaces any existing one for the session)."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("loop prompt is empty")

        now = time.time()
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
        )
        self._state = state
        return self._save()

    @staticmethod
    def _revision_result(*, ok: bool, revision: Optional[Dict[str, Any]], version: int,
                         error_code: str = "", error: str = "") -> Dict[str, Any]:
        return {
            "ok": ok,
            "error_code": error_code,
            "error": error,
            "revision": revision,
            "version": version,
        }

    def _revision_quote(self, state: LoopState, user_quote: str,
                        user_messages: Optional[List[str]], *, required: bool) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
        """Validate a verbatim user quote using the goal lifecycle's shared source rules."""
        from hermes_cli.goals import (
            _REVISION_QUOTE_MIN_CHARS,
            _REVISION_SOURCE_MAX_CHARS,
            user_messages_since,
        )

        quote = " ".join((user_quote or "").split())
        if required and not quote:
            return None, "user_authority_required", {
                "error": (
                    f"this change needs user_quote: a verbatim excerpt "
                    f"({_REVISION_QUOTE_MIN_CHARS}+ chars) of the user's instruction in this session"
                )
            }
        if not quote:
            return "", None, None
        if len(quote) < _REVISION_QUOTE_MIN_CHARS:
            return None, "user_quote_too_short", {
                "error": f"user_quote must be at least {_REVISION_QUOTE_MIN_CHARS} characters"
            }
        pool = user_messages if user_messages is not None else user_messages_since(
            self.session_id, state.created_at
        )
        sources = [" ".join(message.split()) for message in pool if quote in " ".join(message.split())]
        if not sources:
            return None, "user_quote_not_found", {
                "error": "user_quote does not match any user message sent since the loop was set"
            }
        source = next((message for message in sources if len(message) <= _REVISION_SOURCE_MAX_CHARS), "")
        if not source:
            return None, "user_message_too_long", {
                "error": (
                    f"the quoted user message exceeds {_REVISION_SOURCE_MAX_CHARS} characters, too long to "
                    "judge whether it authorizes this change; ask the user to state the change in a short "
                    "message and quote that"
                )
            }
        return quote, None, {"user_message": source}

    def revise(
        self,
        *,
        reason: str,
        actor: str = "agent",
        prompt: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        self_paced: bool = False,
        times: Optional[int] = None,
        until: Optional[str] = None,
        user_quote: str = "",
        user_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Apply a versioned, authority-checked edit to the existing loop."""
        state = self._state
        version = state.version if state is not None else 1
        reason = (reason or "").strip()
        if not reason:
            return self._revision_result(
                ok=False, revision=None, version=version,
                error_code="reason_required", error="a revision needs a reason",
            )
        if state is None or state.status not in {"active", "paused"}:
            return self._revision_result(
                ok=False, revision=None, version=version,
                error_code="no_loop", error="there is no active or paused loop to revise",
            )
        if interval_seconds is not None and self_paced:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code="invalid_cadence",
                error="interval_seconds and self_paced are mutually exclusive",
            )

        before = {
            "prompt": state.prompt,
            "mode": state.mode,
            "interval_seconds": state.interval_seconds,
            "current_delay": state.current_delay,
            "times": state.times,
            "until": state.until,
        }
        after = dict(before)
        clamped_from: Optional[int] = None
        if prompt is not None:
            new_prompt = (prompt or "").strip()
            if new_prompt:
                after["prompt"] = new_prompt
        if interval_seconds is not None:
            requested = int(interval_seconds)
            applied = max(requested, min_interval_seconds())
            clamped_from = requested if applied != requested else None
            after.update({
                "mode": "interval",
                "interval_seconds": float(applied),
                "current_delay": float(applied),
            })
        elif self_paced:
            floor = float(self_paced_floor_seconds())
            after.update({"mode": "self_paced", "interval_seconds": 0.0, "current_delay": floor})
        if times is not None:
            after["times"] = max(0, int(times))
        if until is not None:
            after["until"] = (until or "").strip()

        changed = [key for key in before if after[key] != before[key]]
        if not changed:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code="no_change", error="the revision changes nothing",
            )

        current_effective_delay = (
            state.interval_seconds if state.mode == "interval"
            else (state.current_delay or float(self_paced_floor_seconds()))
        )
        new_effective_delay = (
            after["interval_seconds"] if after["mode"] == "interval"
            else (after["current_delay"] or float(self_paced_floor_seconds()))
        )
        # Switching to self-paced starts at the floor, so it is faster whenever the floor is below the
        # current delay; it gets the same authority check as any other cadence change.
        faster = new_effective_delay < current_effective_delay
        raising_times = (
            "times" in changed
            and state.times > 0
            and (after["times"] == 0 or after["times"] > state.times)
        )
        needs_authority = (
            ("prompt" in changed)
            or ("until" in changed)
            or faster
            or raising_times
        )
        quote, quote_error, quote_detail = self._revision_quote(
            state, user_quote, user_messages, required=needs_authority
        )
        if quote_error:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code=quote_error, error=(quote_detail or {}).get("error", "invalid user quote"),
            )

        now = time.time()
        revision = {
            "at": now,
            "actor": actor,
            "reason": reason,
            "user_quote": quote or "",
            "user_message": (quote_detail or {}).get("user_message", "") if quote else "",
            "before": {key: before[key] for key in changed},
            "after": {key: after[key] for key in changed},
        }
        if clamped_from is not None:
            revision["clamped"] = {
                "interval_seconds": clamped_from,
                "applied": after["interval_seconds"],
            }
        for key in changed:
            setattr(state, key, after[key])
        if any(key in changed for key in ("mode", "interval_seconds", "current_delay")):
            if not state.awaiting_response:
                state.next_due_at = max(
                    now,
                    (state.last_fired_at or now) + new_effective_delay,
                )
        state.revisions.append(revision)
        self._save()
        return self._revision_result(
            ok=True, revision=revision, version=state.version,
        )

    def replace(
        self,
        *,
        prompt: str,
        interval_seconds: Optional[int] = None,
        times: int = 0,
        until: str = "",
        route: Optional[Dict[str, str]] = None,
        reason: str,
        user_quote: str,
        user_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Replace the loop definition while preserving its revision history and route."""
        state = self._state
        version = state.version if state is not None else 1
        reason = (reason or "").strip()
        if not reason:
            return self._revision_result(
                ok=False, revision=None, version=version,
                error_code="reason_required", error="a replacement needs a reason",
            )
        if state is None or state.status not in {"active", "paused"}:
            return self._revision_result(
                ok=False, revision=None, version=version,
                error_code="no_loop", error="there is no active or paused loop to replace",
            )
        prompt = (prompt or "").strip()
        if not prompt:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code="no_change", error="replacement prompt is empty",
            )
        quote, quote_error, quote_detail = self._revision_quote(
            state, user_quote, user_messages, required=True
        )
        if quote_error:
            return self._revision_result(
                ok=False, revision=None, version=state.version,
                error_code=quote_error, error=(quote_detail or {}).get("error", "invalid user quote"),
            )
        now = max(time.time(), state.created_at + 1e-6)
        self_paced = interval_seconds is None
        interval = 0.0 if self_paced else float(max(int(interval_seconds), min_interval_seconds()))
        new_times = max(0, int(times or 0))
        new_until = (until or "").strip()
        old_snapshot = {
            "prompt": state.prompt,
            "mode": state.mode,
            "interval_seconds": state.interval_seconds,
            "current_delay": state.current_delay,
            "times": state.times,
            "until": state.until,
            "ticks_fired": state.ticks_fired,
        }
        new_snapshot = {
            "prompt": prompt,
            "mode": "self_paced" if self_paced else "interval",
            "interval_seconds": interval,
            "current_delay": float(self_paced_floor_seconds()) if self_paced else interval,
            "times": new_times,
            "until": new_until,
            "ticks_fired": 0,
        }
        revision = {
            "at": now,
            "actor": "agent",
            "reason": reason,
            "user_quote": quote or "",
            "user_message": (quote_detail or {}).get("user_message", "") if quote else "",
            "kind": "replace",
            "before": old_snapshot,
            "after": new_snapshot,
        }
        old_route = dict(state.route)
        # Resume is a user-only control: a replaced paused loop stays paused (with its reason) until /loop resume.
        was_paused = state.status == "paused"
        state.prompt = prompt
        state.status = "paused" if was_paused else "active"
        state.mode = new_snapshot["mode"]
        state.interval_seconds = interval
        state.current_delay = new_snapshot["current_delay"]
        state.times = new_times
        state.until = new_until
        state.max_ticks = max_ticks_default()
        state.ticks_fired = 0
        state.created_at = now
        state.last_fired_at = 0.0
        state.next_due_at = now
        state.awaiting_response = False
        state.last_response_digest = ""
        if not was_paused:
            state.paused_reason = None
        state.last_stop_reason = None
        state.route = dict(old_route if route is None else route)
        state.revisions.append(revision)
        self._save()
        return self._revision_result(ok=True, revision=revision, version=state.version)

    def pause(self, reason: str = "user-paused") -> Optional[LoopState]:
        s = self._state
        if not s or s.status not in {"active", "paused"}:
            return None
        s.status, s.paused_reason, s.awaiting_response = "paused", reason, False
        return self._save()

    def resume(self) -> Optional[LoopState]:
        s = self._state
        if not s or s.status == "cleared":
            return None
        s.status, s.paused_reason, s.awaiting_response = "active", None, False
        # Re-arm relative to now so a long pause doesn't fire instantly N times.
        delay = s.current_delay or s.interval_seconds or self_paced_floor_seconds()
        s.next_due_at = time.time() + min(delay, 5.0)
        return self._save()

    def clear(self) -> bool:
        if self._state is None or self._state.status == "cleared":
            return False
        self._state.status = "cleared"
        self._save()
        self._state = None
        return True

    def is_due(self, now: Optional[float] = None) -> bool:
        """Cheap check: active, not mid-wakeup, and the clock has passed."""
        s = self._state
        return (
            s is not None and s.status == "active" and not s.awaiting_response
            and (now if now is not None else time.time()) >= s.next_due_at
        )

    def fire_tick(self) -> Optional[str]:
        """Claim a due tick; returns the message to inject, or None.

        The message is the wakeup-framed prompt, or the raw command when the loop's prompt is
        itself a slash command (``/loop 10m /recap``). Marks ``awaiting_response`` so the tick
        can't double-fire; drivers MUST follow up with ``complete_tick`` (or ``abandon_tick``).
        """
        s = self._state
        if s is None or not self.is_due():
            return None

        def claim(state: LoopState) -> None:
            state.ticks_fired += 1
            state.last_fired_at = time.time()
            state.awaiting_response = True
            # Provisional schedule from NOW: complete_tick reschedules from turn end, but if the
            # process dies mid-turn this keeps the persisted loop from being 'due' in a tight loop.
            state.next_due_at = state.last_fired_at + (
                state.current_delay or state.interval_seconds or self_paced_floor_seconds())

        if not self._claim_tick(claim):
            return None
        s = self._state

        if s.prompt.lstrip().startswith("/"):
            return s.prompt.strip()
        cadence = f", {s.cadence_label()}" if s.mode == "interval" else ", self-paced"
        template = WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE if s.until else WAKEUP_PROMPT_TEMPLATE
        return template.format(tick=s.ticks_fired, cadence=cadence, prompt=s.prompt, until=s.until)

    def _claim_tick(self, claim) -> bool:
        """Claim the due tick on the stored loop inside one write transaction.

        A scheduler loads its manager before awaiting the executor that fires the tick, so a pause,
        stop, revise or re-set can commit in between; saving the cached copy would resurrect it. The
        claim applies only while the stored row is still this loop instance, active and idle. A
        revise in between fires the stored (revised) definition; otherwise the caller's copy fires."""
        s = self._state
        db = _get_session_db() if self._cursor is None else None
        if db is None:
            claim(s)
            self._save()
            return True

        def write(conn) -> Optional[LoopState]:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (_meta_key(self.session_id),)
            ).fetchone()
            stored = _parse_state(row[0]) if row and row[0] else None
            if (
                stored is None or stored.status != "active" or stored.awaiting_response
                or stored.created_at != s.created_at or stored.ticks_fired != s.ticks_fired
            ):
                return None
            base = s if stored.version == s.version else stored
            claimed = _parse_state(base.to_json())
            claim(claimed)
            save_loop(self.session_id, claimed, cursor=conn)
            return claimed

        try:
            claimed = db._execute_write(write)
        except Exception as exc:
            logger.debug("LoopManager: tick claim write failed: %s", exc)
            return False
        if claimed is None:
            self.refresh()
            return False
        self._state = claimed
        return True

    def _save_tick_outcome(self, fired_ticks: int, settle) -> bool:
        """Settle the fired tick on the stored loop inside one write transaction.

        A user or cross-session pause, stop, resume, replace or re-set can land while the tick's
        turn runs; the in-memory copy predates it, and saving that copy would resurrect a stopped
        loop or undo a pause. So the outcome applies only while the stored row is still this
        in-flight tick (active, awaiting_response, same created_at and ticks_fired). A revise keeps
        the tick in flight by design, so ``settle`` runs against the stored row and the revised
        definition survives while the tick still clears awaiting_response."""
        s = self._state
        db = _get_session_db() if self._cursor is None else None
        if s is None:
            return False
        if db is None:
            settle(s)
            self._save()
            return True

        def write(conn) -> Optional[LoopState]:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (_meta_key(self.session_id),)
            ).fetchone()
            stored = _parse_state(row[0]) if row and row[0] else None
            if (
                stored is None or stored.status != "active" or not stored.awaiting_response
                or stored.created_at != s.created_at or stored.ticks_fired != fired_ticks
            ):
                return None
            settle(stored)
            save_loop(self.session_id, stored, cursor=conn)
            return stored

        try:
            settled = db._execute_write(write)
        except Exception as exc:
            logger.debug("LoopManager: tick outcome write failed: %s", exc)
            return False
        if settled is None:
            self.refresh()
            return False
        self._state = settled
        return True

    def _superseded_result(self) -> Dict[str, Any]:
        s = self._state
        return {"status": s.status if s else None, "stopped": False,
                "reason": "loop changed during the tick", "message": ""}

    def abandon_tick(self) -> None:
        """Roll back a fired tick whose injection failed (nothing ran)."""
        s = self._state
        if s is None or not s.awaiting_response:
            return
        fired = s.ticks_fired

        def settle(state: LoopState) -> None:
            state.awaiting_response = False
            state.ticks_fired = max(0, fired - 1)

        self._save_tick_outcome(fired, settle)

    def _stop(self, status: str, reason: str, message: str, *, judged_version: int,
              digest: str = "") -> Dict[str, Any]:
        """Persist a terminal (``done``) or recoverable (``paused``) stop and build the result.

        The verdict was reached against definition ``judged_version``. If a revise changed the
        definition while the tick was being evaluated, the verdict no longer describes the loop:
        settle the tick as a normal continue instead, and the next tick judges the new definition.
        """
        s = self._state
        superseded = False

        def settle(state: LoopState) -> None:
            nonlocal superseded
            if state.version != judged_version:
                superseded = True
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
        if superseded:
            return {"status": "active", "stopped": False,
                    "reason": "loop revised during evaluation", "message": ""}
        return {"status": status, "stopped": True, "reason": reason, "message": message}

    @staticmethod
    def _settle_continue(state: LoopState, digest: str, now: float) -> None:
        """Clear the in-flight tick and schedule the next one on the stored cadence."""
        state.awaiting_response = False
        if state.mode == "self_paced":
            floor = self_paced_floor_seconds()
            if digest and digest == state.last_response_digest:
                state.current_delay = min(max(state.current_delay, floor) * 2, self_paced_ceiling_seconds())
            else:
                state.current_delay = float(floor)
            state.last_response_digest = digest
        else:
            state.current_delay = state.interval_seconds
        state.next_due_at = now + state.current_delay

    def complete_tick(self, last_response: str) -> Dict[str, Any]:
        """Evaluate the finished wakeup turn and schedule what's next.

        Returns ``{"status": "active|done|paused", "stopped": bool, "reason": str, "message": str}``;
        ``message`` is a user-visible one-liner, "" in the common still-looping case.
        """
        # Decide on the stored loop: a pause, stop or revise during the turn must govern this tick.
        self.refresh()
        s = self._state
        if s is None or not s.awaiting_response:
            return {"status": s.status if s else None, "stopped": False, "reason": "no tick in flight", "message": ""}
        now = time.time()
        ticks = _ticks_label(s.ticks_fired)
        # Verdicts below judge this definition; a revise landing mid-evaluation supersedes them.
        judged = {"judged_version": s.version, "digest": _digest_response(last_response)}

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
                verdict, reason = "continue", f"judge unavailable: {type(exc).__name__}"
            if verdict == "done":
                return self._stop("done", f"stop condition met: {reason}",
                                  f"✓ Loop finished after {ticks} — {reason}", **judged)
            if verdict == "blocked":
                # Unachievable stop condition: pause so the user can re-scope, don't spin.
                why = f"stop condition judged unachievable: {reason}"
                return self._stop("paused", why,
                                  f"⏸ Loop paused — {why}. /loop resume to keep going, /loop stop to end it.", **judged)

        # 3. --times user cap.
        if s.times and s.ticks_fired >= s.times:
            return self._stop("done", f"completed the requested {s.times} runs",
                              f"✓ Loop finished — ran {s.times}/{s.times} times.", **judged)

        # 4. Config backstop budget → pause (recoverable), not done.
        if s.max_ticks and s.ticks_fired >= s.max_ticks:
            return self._stop(
                "paused", f"tick budget exhausted ({s.ticks_fired}/{s.max_ticks})",
                f"⏸ Loop paused — {s.ticks_fired}/{s.max_ticks} ticks used "
                "(loops.max_ticks). /loop resume to keep going, /loop stop to end it.",
                **judged,
            )

        # 5. Still looping — schedule the next tick from turn end, using the stored cadence so a
        # revise that landed during this tick takes effect now.
        def settle(state: LoopState) -> None:
            self._settle_continue(state, judged["digest"], now)

        if not self._save_tick_outcome(s.ticks_fired, settle):
            return self._superseded_result()
        return {"status": "active", "stopped": False, "reason": "loop continues", "message": ""}


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


def _pause_output(mgr: "LoopManager") -> str:
    state = mgr.pause(reason="user-paused")
    return "No loop set." if state is None else f"⏸ Loop paused: {state.prompt}\nUse /loop resume to continue."


def _resume_output(mgr: "LoopManager") -> str:
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
    mgr: "LoopManager",
    args: str,
    *,
    route: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
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
    "response_signals_complete", "goal_blocks_loop_tick", "load_loop", "save_loop", "clear_loop",
    "list_active_loops", "migrate_loop_to_session", "dispatch_loop_command", "LOOP_COMPLETE_MARKER",
    "WAKEUP_PROMPT_TEMPLATE", "WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE", "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEFAULT_MAX_TICKS", "is_quiet_wakeup_prompt",
]
