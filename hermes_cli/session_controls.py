"""Cross-session goal and loop controls with durable approval records."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple

from hermes_cli.goals import (
    _REPLY_QUOTE_RE,
    _REVISION_QUOTE_MIN_CHARS,
    _REVISION_SOURCE_MAX_CHARS,
    _get_session_db,
    _is_user_typed,
)

logger = logging.getLogger(__name__)

_REQUEST_TTL_SECONDS = 24 * 60 * 60
_CONTROL_PREFIX = "session_control:"
_CURRENT_AUTHORITY: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "session_control_authority", default=None
)


def _current_profile() -> str:
    try:
        from hermes_cli.profiles import current_profile_name
        return str(current_profile_name("default") or "default")
    except Exception:
        return "default"


def _db():
    return _get_session_db()


# A replacement (objective plus contract) must be shown whole on the approval card (one Telegram
# message), so a longer one is refused rather than clipped where the user could approve text they
# never saw.
REPLACEMENT_MAX_CHARS = 1500


_REPLACE_PAYLOAD_KEYS = frozenset({"goal", "contract", "max_turns"})


def replacement_contract(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """The contract a replacement installs (missing fields are empty: the old contract is dropped)."""
    from hermes_cli.goals import _CONTRACT_FIELDS
    raw = (payload or {}).get("contract")
    raw = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw or {})
    return {f: str(raw.get(f) or "").strip() for f in _CONTRACT_FIELDS}


def _check_payload(kind: str, action: str, payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate a payload and return it normalized exactly as it will be stored, shown and applied."""
    if kind == "goal" and action == "replace":
        payload = dict(payload or {})
        if set(payload) - _REPLACE_PAYLOAD_KEYS:
            raise ValueError("unsupported_replacement_field")  # the card could not show it
        new = str(payload.get("goal") or "").strip()
        if not new:
            raise ValueError("replacement_goal_required")
        shown = len(new) + sum(len(v) for v in replacement_contract(payload).values())
        if shown > REPLACEMENT_MAX_CHARS:
            raise ValueError("replacement_too_long")
        max_turns = payload.get("max_turns")
        if max_turns is not None and (
                isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1):
            raise ValueError("invalid_max_turns")  # shown exactly as approval installs it
        normalized: Dict[str, Any] = {"goal": new}
        if "contract" in payload:
            normalized["contract"] = replacement_contract(payload)
        if max_turns is not None:
            normalized["max_turns"] = max_turns
        return normalized
    return payload


def _record_key(request_id: str) -> str:
    return f"{_CONTROL_PREFIX}{request_id}"


def _now() -> float:
    return time.time()


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def resolve_target(target: str) -> str:
    """Resolve a bare session id or ``hermes:<profile>/<session_id>``."""
    raw = str(target or "").strip()
    if raw.startswith("hermes:"):
        value = raw[len("hermes:"):]
        if "/" not in value:
            raise ValueError("unknown_target")
        profile, sid = value.split("/", 1)
        if profile != _current_profile():
            raise ValueError("cross_profile")
        raw = sid
    if not raw:
        raise ValueError("unknown_target")
    db = _db()
    if db is None or not db.get_session(raw):
        raise ValueError("unknown_target")
    return raw


# Deterministic negation guard: a quoted span preceded (earlier in its sentence) by, or containing, one
# of these tokens is refused so "do NOT clear the goal" cannot authorize a clear. "stop" is not a
# negation: it is itself a control verb.
_NEGATION_TOKENS = frozenset({"not", "don't", "dont", "never", "no", "shouldn't", "shouldnt",
                              "can't", "cant", "cannot", "won't", "wont", "didn't", "didnt", "doesn't"})


_WORD_RE = re.compile(r"[a-z0-9']+")


def _negation_words(text: str) -> list[str]:
    # Any character other than a letter, digit or apostrophe separates words, so emphasis
    # ("do**not**"), dashes ("not—under"), slashes and other punctuation can never glue a negation
    # onto its neighbour and hide it.
    lowered = str(text or "").replace("\u2019", "'").replace("\u2018", "'").lower()
    return [w.strip("'") for w in _WORD_RE.findall(lowered) if w.strip("'")]


_CLAUSE_BREAK = re.compile(r"[.!?;\n]+")


def _is_negated(quote: str, source: str) -> bool:
    """True when the quote contains a negation, or one appears earlier in its sentence.

    Conservative by design: a refused quote only falls back to an Approve/Deny request, so any
    negation before the quoted span in the same sentence ("do not, under any circumstances,
    clear ...") refuses it. Only a sentence break (. ! ? ; newline) ends the governing clause.
    """
    def negates(words: list[str]) -> bool:
        return any(w in _NEGATION_TOKENS for w in words)

    if negates(_negation_words(quote)):
        return True
    start = 0
    while True:
        idx = source.find(quote, start)
        if idx < 0:
            return False
        prefix = source[:idx]
        if idx and not source[idx - 1].isspace():
            # The quote starts mid-word ("t clear ..." inside "don't clear ..."): judge the whole word.
            prefix += quote.split(" ", 1)[0]
        clause = _CLAUSE_BREAK.split(prefix)[-1]
        if negates(_negation_words(clause)):
            return True
        start = idx + 1


# Words that make a message something other than a plain, unconditional instruction: conditions,
# timing, hedges and questions. A message containing any of them (or a "?") never grants quote
# authority, whatever span is quoted, so "I would clear the X goal only if the deploy is green" and
# "Please explain how to clear the X goal" fall back to Approve/Deny.
_QUALIFIER_TOKENS = frozenset({
    "if", "unless", "only", "when", "whenever", "once", "after", "before", "until", "later",
    "whether", "how", "why", "what", "would", "could", "should", "might", "maybe",
    "perhaps", "probably", "explain", "wait", "think", "wonder", "consider",
})
_SENTENCE_END = ".!;\n"


def _governing_sentence(quote: str, source: str) -> str:
    """The whole sentence(s) of ``source`` containing the first occurrence of ``quote``.

    Authority is judged on the full sentence, never the agent-chosen span inside it, so a
    control-looking fragment cannot be lifted out of a sentence that also names other work."""
    idx = source.find(quote)
    if idx < 0:
        return quote
    start = max(source.rfind(ch, 0, idx) for ch in _SENTENCE_END + "?") + 1
    body_end = idx + len(quote.rstrip(_SENTENCE_END + "? "))
    ends = [pos for pos in (source.find(ch, body_end) for ch in _SENTENCE_END + "?") if pos >= 0]
    end = min(ends) if ends else len(source)
    return source[start:end].strip()


_REPLY_QUOTE_SHORTEST_RE = re.compile(r'^\[Replying to(?: your previous message)?: ".*?"\]\n\s*', re.DOTALL)
# Quotation marks, code spans and block quotes mark quoted data (an example, a log, a prompt being
# discussed), not an instruction, so a message containing any of them grants no quote authority.
# An ASCII single quote at a word edge ('...') is a quotation; inside a word it is an apostrophe
# (don't, user's), which stays allowed. Conservative: a trailing plural possessive also refuses.
_QUOTED_DATA_RE = re.compile(
    r'["\u201c\u201d\u2018\u2019\u201a\u201e`]|^\s*>|(?<![A-Za-z0-9])\'|\'(?![A-Za-z0-9])', re.MULTILINE)


def _message_grants_authority(source: str) -> bool:
    """True when the whole message is free of refusals, questions, conditions, hedges and quoted data."""
    words = _negation_words(source)
    if "?" in source or _QUOTED_DATA_RE.search(source):
        return False
    return not any(w in _NEGATION_TOKENS or w in _QUALIFIER_TOKENS for w in words)


def check_user_quote(requester_session_id: str, quote: str, *, cursor=None) -> Tuple[str, str] | str:
    """Validate a quote against only the requester's latest typed user message."""
    normalized = _normalize(quote)
    if len(normalized) < _REVISION_QUOTE_MIN_CHARS:
        return "user_quote_too_short"
    db = _db()
    if db is None:
        return "user_quote_not_found"
    try:
        # Only what the conversation currently says: a rewound (/undo) instruction never authorizes.
        rows = db.messages_by_role(requester_session_id, "user", since=0.0, limit=500, cursor=cursor,
                                   current_only=True)
    except Exception:
        return "user_quote_not_found"
    latest = next((row for row in rows if _is_user_typed(row)), None)
    if latest is None:
        return "user_quote_not_found"
    source_raw = str(latest.get("content") or "")
    try:
        from gateway.response_filters import is_agent_origin_text
        if is_agent_origin_text(source_raw):
            return "user_quote_not_found"
    except ValueError:
        raise
    except Exception:
        return "user_quote_not_found"
    # The gateway reply pointer repeats the assistant's words; only the user's own text authorizes.
    # Its end is ambiguous when the quoted words themselves contain the closing marker, and then
    # either cut could misattribute text (drop a refusal, or treat the assistant's words as the
    # user's), so an ambiguous pointer grants no quote authority.
    stripped = _REPLY_QUOTE_RE.sub("", source_raw, count=1)
    if stripped != source_raw and _REPLY_QUOTE_SHORTEST_RE.sub("", source_raw, count=1) != stripped:
        return "user_quote_ambiguous"
    source = _normalize(stripped)
    if len(source) > _REVISION_SOURCE_MAX_CHARS:
        return "user_message_too_long"
    if normalized not in source:
        return "user_quote_not_found"
    if _is_negated(normalized, source):
        return "user_quote_negated"
    # Authority comes from the user's instruction in its full context, never a trimmed substring:
    # any refusal, question or condition anywhere in the message ("Clear the X goal? Not now")
    # refuses. Callers match the control against the quote's whole sentence (_governing_sentence).
    # Quoted-data provenance (block quotes, fences) is line-based: judge it on the raw message,
    # before normalization collapsed the newlines that mark it.
    if not _message_grants_authority(source) or _QUOTED_DATA_RE.search(stripped):
        return "user_quote_qualified"
    # The whole message must be the one instruction. Any other sentence can revoke, correct or
    # reframe it ("Disregard the next sentence. Clear ...", "Clear ... Actually, keep it"), and no
    # word list can read that reliably, so a multi-sentence message falls back to Approve/Deny.
    if len([part for part in _CLAUSE_BREAK.split(source) if part.strip()]) != 1:
        return "user_quote_multi_sentence"
    return normalized, source


# A quote authorizes only the control it states. Each control has disjoint verb sets within its
# kind, the quote must name the kind, and a cross-session quote must name the target session, so
# "pause the goal" never authorizes a clear and "pause the goal or clear it" (two actions)
# authorizes neither. Anything ambiguous falls back to an Approve/Deny request.
_KIND_WORDS = {"goal": frozenset({"goal", "goals", "objective"}), "loop": frozenset({"loop", "loops"})}
_ACTION_VERBS = {
    "goal": {
        "clear": frozenset({"clear", "remove", "drop", "delete", "cancel", "end", "stop", "kill"}),
        "pause": frozenset({"pause", "hold", "suspend"}),
        "resume": frozenset({"resume", "unpause", "continue", "restart"}),
        "replace": frozenset({"replace", "change", "switch", "update", "swap"}),
    },
    "loop": {
        "stop": frozenset({"stop", "end", "kill", "cancel", "clear", "remove", "delete", "drop"}),
        "pause": frozenset({"pause", "hold", "suspend"}),
        "resume": frozenset({"resume", "unpause", "continue", "restart"}),
    },
}


def _bounded_in(needle: str, haystack: str) -> bool:
    """True when ``needle`` appears in ``haystack`` as whole words, not inside a longer word."""
    return bool(needle) and re.search(
        r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack) is not None


def _quote_names_target(quote: str, target_sid: str, *, cursor=None) -> bool:
    """True when the quote names exactly this session: its id, or a title no other session shares.

    A title counts only as whole words, and only when no other session's title that also appears in
    the quote equals or contains it ("Clear the Deploy v2 goal" names "Deploy v2", never "Deploy").
    Anything ambiguous returns False, so the control falls back to an Approve/Deny request."""
    lowered = _normalize(quote).lower()
    if _bounded_in(target_sid.lower(), lowered):
        return True
    title = _normalize(_session_title(target_sid, cursor=cursor)).lower()
    if len(title) < 3 or not _bounded_in(title, lowered):
        return False
    # Every other titled session is compared in Python with the same folding as the target: a SQL
    # lower() prefilter folds only ASCII, so "CAFÉ search" would escape the ambiguity check.
    sql = "SELECT id, title FROM sessions WHERE id != ? AND title IS NOT NULL AND title != ''"
    params = (target_sid,)
    try:
        if cursor is not None:
            rows = cursor.execute(sql, params).fetchall()
        else:
            db = _db()
            rows = db._read_all(sql, params) if db is not None else []
    except Exception:
        logger.debug("session title disambiguation failed", exc_info=True)
        return False
    for row in rows:
        other = _normalize(row[1] if not hasattr(row, "keys") else row["title"]).lower()
        if other and _bounded_in(other, lowered) and title in other:
            return False
    return True


# A direct instruction has a fixed shape: optional courtesy words, the action verb, a noun phrase
# naming only the target ("the Deploy goal", "this goal"), then only filler, or for a replacement
# optionally "with/to <new objective>". Anything else ("Keep the X goal and clear the Y goal", "Print the text
# clear the X goal") is not the user telling us to do this control, so it falls back to Approve/Deny.
_LEAD_WORDS = frozenset({"please", "pls", "kindly", "yes", "yeah", "yep", "ok", "okay", "sure", "go",
                         "ahead", "and", "just", "now", "so", "alright", "right", "thanks", "then"})
_DETERMINERS = frozenset({"the", "this", "that", "my", "our", "its", "your", "current", "active",
                          "existing", "running", "session's", "topic's"})
_TAIL_WORDS = frozenset({"now", "immediately", "please", "right", "away", "asap", "today", "too",
                         "thanks", "thank", "you", "entirely", "completely", "fully", "altogether",
                         "for", "good", "as", "well", "then", "already", "again"})
_REPLACE_LINKS = frozenset({"with", "to", "into"})


def _target_name_phrases(target_sid: str, *, cursor=None) -> list[list[str]]:
    phrases = [_negation_words(target_sid)]
    title = _negation_words(_normalize(_session_title(target_sid, cursor=cursor)))
    if title:
        phrases.append(title)
    return [p for p in phrases if p]


def _is_direct_instruction(kind: str, action: str, target_sid: str, requester_sid: str,
                           sentence: str, *, cursor=None) -> bool:
    words = _negation_words(sentence)
    verbs = _ACTION_VERBS.get(kind, {}).get(action, frozenset())
    kind_words = _KIND_WORDS.get(kind, frozenset())
    head = words
    if action == "replace":
        # The new objective after "with"/"to" is free text and may itself say "goal".
        link = next((n for n, w in enumerate(words) if w in _REPLACE_LINKS), len(words))
        head = words[:link]
    if sum(1 for w in head if w in kind_words) != 1:
        return False  # one sentence naming two goals (or loops) is a multi-target clause
    i = 0
    while i < len(words) and words[i] in _LEAD_WORDS and words[i] not in verbs:
        i += 1
    if i >= len(words) or words[i] not in verbs:
        return False
    k = words.index(next(w for w in words if w in kind_words))
    if k <= i:
        return False
    phrase = [w for w in words[i + 1:k] if w not in _DETERMINERS]
    names = _target_name_phrases(target_sid, cursor=cursor)
    if target_sid != requester_sid:
        if phrase not in names:
            return False
    elif phrase and phrase not in names:
        return False
    tail = words[k + 1:]
    if action == "replace" and tail and tail[0] in _REPLACE_LINKS:
        return len(tail) > 1  # "with/to <new objective>"; the card shows the objective in full
    return all(w in _TAIL_WORDS for w in tail)


def _quote_matches_control(kind: str, action: str, target_sid: str, requester_sid: str,
                           quote: str, payload: Optional[Dict[str, Any]], *, cursor=None) -> bool:
    """True when the quoted words state this exact control: kind, action, target and payload."""
    all_words = _negation_words(quote)
    if kind == "goal" and action == "replace":
        link = next((n for n, w in enumerate(all_words) if w in _REPLACE_LINKS), len(all_words))
        all_words = all_words[:link]
    words = set(all_words)
    if not words & _KIND_WORDS.get(kind, frozenset()):
        return False
    if any(words & other for name, other in _KIND_WORDS.items() if name != kind):
        return False  # one sentence naming both a goal and a loop is ambiguous
    stated = {name for name, verbs in _ACTION_VERBS.get(kind, {}).items() if words & verbs}
    if stated != {action}:
        return False
    if target_sid != requester_sid and not _quote_names_target(quote, target_sid, cursor=cursor):
        return False
    if not _is_direct_instruction(kind, action, target_sid, requester_sid, quote, cursor=cursor):
        return False
    # A replacement's new objective is agent-authored from the user's request (the user asks for a
    # better goal; the agent writes it), so the quote binds the replace intent and target, and the
    # revision records the quote beside the new objective for audit.
    return True


def _load_record(request_id: str) -> Optional[Dict[str, Any]]:
    db = _db()
    if db is None:
        return None
    raw = db.get_meta(_record_key(request_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _save_record(record: Dict[str, Any], *, cursor=None) -> None:
    db = _db()
    if db is None:
        raise RuntimeError("session database unavailable")
    db.set_meta(_record_key(record["id"]), json.dumps(record, ensure_ascii=False), cursor=cursor)


def _manager_apply(kind: str, action: str, target_sid: str, *, reason: str,
                   payload: Optional[Dict[str, Any]] = None,
                   authority: Optional[Dict[str, Any]] = None, cursor=None) -> Dict[str, Any]:
    payload = dict(payload or {})
    handler = CONTROLS.get((kind, action))
    if action == "replace" and kind == "goal":
        from hermes_cli.goals import GoalManager
        manager = GoalManager(target_sid, cursor=cursor)
        with _approved_authority(authority):
            result = manager.replace(
                reason=reason or "session-control",
                goal=str(payload.get("goal") or ""),
                max_turns=payload.get("max_turns"),
                contract=payload.get("contract"),
                user_quote=str((authority or {}).get("quote") or ""),
                user_messages=[str((authority or {}).get("message") or "")],
            )
        continuation_prompt = manager.next_continuation_prompt() if result.get("ok") else None
        return {"result": result, "state": getattr(manager, "state", None),
                "continuation_prompt": continuation_prompt}
    if kind == "goal" and action == "resume":
        from hermes_cli.goals import GoalManager
        manager = GoalManager(target_sid, cursor=cursor)
        # Only a paused goal resumes: never revive a done/cleared goal or re-arm an active one.
        if manager.state is None or manager.state.status != "paused":
            code = f"nothing_to_{action}"
            return {"result": {"ok": False, "error_code": code, "error": code},
                    "state": manager.state}
        result = manager.resume()
        if result is None:
            code = f"nothing_to_{action}"
            return {"result": {"ok": False, "error_code": code, "error": code},
                    "state": getattr(manager, "state", None)}
        return {"result": result, "state": getattr(manager, "state", None),
                "continuation_prompt": manager.next_continuation_prompt()}
    if not callable(handler):
        raise ValueError("unsupported_control")
    before_state = None
    if kind == "goal":
        from hermes_cli.goals import load_goal
        before_state = load_goal(target_sid, cursor=cursor)
        # A cleared goal stays stored; pausing or clearing it again must not revive or re-announce it.
        if before_state is None or getattr(before_state, "status", None) not in {"active", "paused"}:
            code = f"nothing_to_{action}"
            return {"result": {"ok": False, "error_code": code, "error": code}, "state": before_state}
    result = handler(target_sid, reason, payload, authority, cursor)
    if kind == "goal":
        from hermes_cli.goals import load_goal
        state = load_goal(target_sid, cursor=cursor)
    elif kind == "loop":
        from hermes_cli.loops import load_loop
        state = load_loop(target_sid, cursor=cursor)
    else:
        raise ValueError("unsupported_control")
    cleared_goal = (
        kind == "goal" and action == "clear" and before_state is not None
        and getattr(before_state, "status", None) != "cleared"
        and state is not None and getattr(state, "status", None) == "cleared"
    )
    if result is False or (result is None and not cleared_goal):
        code = f"nothing_to_{action}"
        return {"result": {"ok": False, "error_code": code, "error": code}, "state": state}
    return {"result": result, "state": state}


def _session_title(session_id: str, *, cursor=None) -> str:
    db = _db()
    if db is None:
        return ""
    try:
        if cursor is not None:
            row = cursor.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return str(row[0] or "") if row else ""
        return str(db.get_session_title(session_id) or "")
    except Exception:
        return ""


def _affected_text(kind: str, action: str, raw: Optional[str], payload: Optional[Dict[str, Any]] = None) -> str:
    payload = payload or {}
    try:
        if kind == "goal":
            from hermes_cli.goals import GoalState
            state = GoalState.from_json(raw) if raw else None
            old = str(getattr(state, "goal", "") or "")
            if action == "replace":
                return f"goal: {old or '(none)'} -> {str(payload.get('goal') or '')}"
            return f"goal: {old or '(none)'}"
        if kind == "loop":
            from hermes_cli.loops import LoopState
            state = LoopState.from_json(raw) if raw else None
            prompt = str(getattr(state, "prompt", "") or "")
            cadence = state.cadence_label() if state is not None else ""
            return f"loop: {prompt or '(none)'}{f' ({cadence})' if cadence else ''}"
    except (TypeError, ValueError, AttributeError):
        logger.debug("could not describe session-control target", exc_info=True)
    return ""


def _definition_fingerprint(kind: str, raw: Optional[str]) -> str:
    """Bind the authored definition, not progress counters or scheduler bookkeeping.

    v2 also covers contracts/revisions and cadence/conditions. Old text-only cards
    cannot authorize a v2 definition and must be requested again.
    """
    if not raw or kind not in {"goal", "loop"}:
        return ""
    try:
        data = json.loads(raw)
        text_key = "goal" if kind == "goal" else "prompt"
        if not isinstance(data, dict) or not isinstance(data.get(text_key), str) or not data[text_key]:
            return ""
        fields = (
            ("goal", "created_at", "max_turns", "contract", "subgoals", "revisions") if kind == "goal"
            else ("prompt", "created_at", "mode", "interval_seconds", "times", "until", "max_ticks", "route", "revisions")
        )
        definition = {field: data.get(field) for field in fields}
        if kind == "goal":
            definition["gates"] = [
                {field: gate.get(field) for field in ("command", "timeout_seconds", "max_retries")}
                for gate in (data.get("gates") or [])
            ]
        encoded = json.dumps(definition, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return "v2:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, AttributeError):
        return ""


def _goal_continuation_matches(metadata: Any, session_id: str, definition: Any) -> bool:
    """Match an active goal instance; authored edits are refreshed at turn consumption."""
    if not isinstance(metadata, dict) or not isinstance(definition, dict):
        return False
    sid = metadata.get("goal_continuation_session_id")
    fingerprint = metadata.get("goal_continuation_fingerprint")
    instance = metadata.get("goal_continuation_instance")
    return bool(
        isinstance(sid, str) and sid and (not session_id or sid == session_id)
        and isinstance(fingerprint, str) and fingerprint.startswith("v2:")
        and type(instance) in (int, float) and instance > 0
        and definition.get("status") == "active" and instance == definition.get("created_at")
    )


def goal_continuation_is_current(metadata: Any, session_id: str = "", *, cursor=None) -> bool:
    """One fail-closed instance fence for every goal continuation, regardless of its producer."""
    if not isinstance(metadata, dict):
        return False
    sid = metadata.get("goal_continuation_session_id")
    if not isinstance(sid, str) or not sid or (session_id and sid != session_id):
        return False
    try:
        db = _db() if cursor is None else None
        raw = _meta_value(cursor, _definition_key("goal", sid)) if cursor is not None else (
            db.get_meta(_definition_key("goal", sid)) if db is not None else None
        )
        return _goal_continuation_matches(metadata, session_id, json.loads(raw) if raw else None)
    except Exception:
        logger.debug("goal continuation instance check failed", exc_info=True)
        return False


LOOP_WAKE_METADATA_KEY = "loop_wakeup"


def loop_wake_metadata(session_id: str, state: Any, *, command: bool = False) -> Dict[str, Any]:
    """Identity stamped on a loop wake: the loop instance and the tick it claimed.

    ``command`` marks a slash-command tick, which the scheduler completes as soon as the command is
    handed to the adapter, so its fence cannot require the tick to still be awaiting a response."""
    return {LOOP_WAKE_METADATA_KEY: True, "loop_wakeup_session_id": session_id,
            "loop_wakeup_instance": getattr(state, "created_at", None),
            "loop_wakeup_tick": getattr(state, "ticks_fired", None),
            "loop_wakeup_command": bool(command)}


def loop_wake_is_current(metadata: Any, session_id: str = "", *, cursor=None) -> bool:
    """True unless this is a loop wake whose claimed tick is no longer the live, running tick.

    A pause, stop, re-set or completed tick between the claim and the turn start means the wake
    is obsolete work and must not run. An unreadable store admits the wake: every pause, stop or
    re-set is itself a committed write to that store, and dropping a claimed tick there would
    leave it awaiting a response forever."""
    if not isinstance(metadata, dict) or not metadata.get(LOOP_WAKE_METADATA_KEY):
        return True
    sid = metadata.get("loop_wakeup_session_id")
    if not isinstance(sid, str) or not sid or (session_id and sid != session_id):
        return False
    try:
        db = _db() if cursor is None else None
        if cursor is None and db is None:
            return True
        raw = _meta_value(cursor, _definition_key("loop", sid)) if cursor is not None else (
            db.get_meta(_definition_key("loop", sid))
        )
        stored = json.loads(raw) if raw else None
    except Exception:
        logger.debug("loop wake identity check could not read the store", exc_info=True)
        return True
    if stored is None:
        # Loop rows are never deleted (a stop marks them cleared), so a missing row means no
        # pause, stop or re-set was ever written there: nothing superseded this wake.
        return True
    if not (isinstance(stored, dict)
            and stored.get("created_at") == metadata.get("loop_wakeup_instance")
            and stored.get("ticks_fired") == metadata.get("loop_wakeup_tick")):
        return False
    if metadata.get("loop_wakeup_command"):
        # A command tick is completed right after admission. That completion may itself end the
        # loop at its run cap (done) or tick budget (paused, with the budget reason), and the
        # final requested run must still execute. A user or session-control pause, or a stop,
        # writes a different status or reason and drops the command.
        status = stored.get("status")
        if status == "active":
            return True
        if status == "done":
            return str(stored.get("last_stop_reason") or "").startswith("completed the requested")
        if status == "paused":
            return str(stored.get("paused_reason") or "").startswith("tick budget exhausted")
        return False
    return bool(stored.get("status") == "active" and stored.get("awaiting_response"))


def is_gateway_wake(event: Any) -> bool:
    """A goal continuation or loop tick, recognized by its stamp (or legacy continuation text)."""
    from gateway.platforms.event import is_goal_continuation_event
    metadata = getattr(event, "metadata", None)
    return bool(is_goal_continuation_event(event)
                or (isinstance(metadata, dict) and metadata.get(LOOP_WAKE_METADATA_KEY)))


def internal_wake_is_current(event: Any, session_id: str = "") -> bool:
    """One fence for gateway-authored wakes: goal continuations and loop ticks."""
    from gateway.platforms.event import is_goal_continuation_event
    metadata = getattr(event, "metadata", None)
    if is_goal_continuation_event(event) and not goal_continuation_is_current(metadata, session_id):
        return False
    return loop_wake_is_current(metadata, session_id)


def refresh_goal_continuation(event: Any, session_id: str) -> Optional[str]:
    """Validate and refresh the queued turn input from one current GoalManager snapshot."""
    from hermes_cli.goals import GoalManager

    try:
        metadata = getattr(event, "metadata", None)
        manager = GoalManager(session_id)
        raw = manager.state.to_json() if manager.state is not None else None
        if not _goal_continuation_matches(metadata, session_id, json.loads(raw) if raw else None):
            return None
        fingerprint = _definition_fingerprint("goal", raw)
        if fingerprint != metadata["goal_continuation_fingerprint"]:
            prompt = manager.next_continuation_prompt()
            if prompt is None:
                return None
            event.text = prompt
            metadata["goal_continuation_fingerprint"] = fingerprint
        return event.text
    except Exception:
        logger.debug("goal continuation refresh failed", exc_info=True)
        return None


def _meta_value(cursor, key: str) -> Optional[str]:
    row = cursor.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None

def _definition_key(kind: str, session_id: str) -> str:
    return f"{'goal' if kind == 'goal' else 'loop'}:{session_id}"


def _bind_continuation(record: Dict[str, Any], result: Dict[str, Any], cursor) -> None:
    if result.get("continuation_prompt"):
        record["continuation_prompt"] = result["continuation_prompt"]
        raw = _meta_value(cursor, _definition_key(record["kind"], record["target_session_id"]))
        record["continuation_fingerprint"] = _definition_fingerprint(record["kind"], raw)
        if record["kind"] == "goal":
            record["continuation_instance"] = json.loads(raw)["created_at"]

def _new_record(kind: str, action: str, target_sid: str, requester_sid: str, *,
                reason: str, payload: Optional[Dict[str, Any]], authority: Dict[str, Any],
                status: str, now: float, expires_at: Optional[float], affected_text: str, cursor=None) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12], "kind": kind, "action": action,
        "target_session_id": target_sid, "requester_session_id": requester_sid,
        "requester_title": _session_title(requester_sid, cursor=cursor), "target_title": _session_title(target_sid, cursor=cursor),
        "reason": reason or "session-control", "payload": dict(payload or {}),
        "affected_text": affected_text,
        "authority": dict(authority), "status": status, "created_at": now,
        "expires_at": expires_at, "resolved_at": None, "error": None,
        "request_posted": status != "pending", "request_skipped": False,
        "target_notice_sent": False, "target_notice_skipped": False,
        "requester_notified": False, "requester_notification_skipped": False,
        "continuations_cleared": False, "continuation_enqueued": False,
        "outbox_done": False,
    }


@contextmanager
def _approved_authority(authority: Optional[Dict[str, Any]]):
    token = _CURRENT_AUTHORITY.set(authority if authority else None)
    try:
        yield
    finally:
        _CURRENT_AUTHORITY.reset(token)


# Platforms whose adapter renders Approve/Deny buttons for ``send_control_request``.
_APPROVABLE_SOURCES = frozenset({"telegram"})


def _session_source(session_id: str) -> str:
    db = _db()
    row = db.get_session(session_id) if db is not None else None
    return str((row or {}).get("source") or "").strip().lower()


def _has_approval_surface(session_id: str) -> bool:
    return _session_source(session_id) in _APPROVABLE_SOURCES


def _has_gateway_route(session_id: str) -> bool:
    db = _db()
    if db is None:
        return False
    row = db.get_session(session_id)
    if not row:
        return False
    source = str(row.get("source") or "").strip().lower()
    chat_id = row.get("chat_id")
    if not source or chat_id in (None, ""):
        return False
    try:
        from gateway.config import Platform
        platform = Platform(source)
    except (TypeError, ValueError):
        return False
    return platform.value != "local"


def request_control(kind: str, action: str, target_sid: str, *, requester_sid: str,
                    reason: str = "", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a durable, 24-hour pending approval request."""
    if (kind, action) not in CONTROLS:
        raise ValueError("unsupported_control")
    payload = _check_payload(kind, action, payload)
    target_sid = resolve_target(target_sid)
    if not _has_gateway_route(target_sid):
        return {"ok": False, "error_code": "target_unroutable", "error": "target_unroutable"}
    if not _has_approval_surface(target_sid):
        return {"ok": False, "error_code": "target_unapprovable", "error": "target_unapprovable"}
    now = _now()
    db = _db()

    def create(conn):
        # Card description and binding come from one SQLite snapshot, never two manager loads.
        raw = _meta_value(conn, _definition_key(kind, target_sid))
        record = _new_record(kind, action, target_sid, requester_sid, reason=reason, payload=payload,
                             authority={"via": "button", "user_id": None}, status="pending", now=now,
                             expires_at=now + _REQUEST_TTL_SECONDS,
                             affected_text=_affected_text(kind, action, raw, payload), cursor=conn)
        record["target_fingerprint"] = _definition_fingerprint(kind, raw)
        _save_record(record, cursor=conn)
        return record

    return db._execute_write(create)


def apply_control(kind: str, action: str, target_sid: str, *, requester_sid: str,
                  reason: str = "", user_quote: str = "",
                  payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Apply an authorized control immediately, or create an approval request."""
    if (kind, action) not in CONTROLS:
        return {"ok": False, "error_code": "unsupported_control"}
    try:
        payload = _check_payload(kind, action, payload)
        target_sid = resolve_target(target_sid)
        if not _has_gateway_route(target_sid):
            raise ValueError("target_unroutable")
    except ValueError as exc:
        return {"ok": False, "error_code": str(exc), "error": str(exc)}
    def request_approval(refused: str) -> Dict[str, Any]:
        # A missing or refused quote (stale, paraphrased, relayed, negated, or overtaken by a newer
        # message before the write lock) never authorizes the control; it falls back to an
        # Approve/Deny request so Brian decides.
        try:
            record = request_control(kind, action, target_sid, requester_sid=requester_sid,
                                     reason=reason, payload=payload)
        except ValueError as exc:
            return {"ok": False, "error_code": str(exc), "error": str(exc)}
        if not record.get("id"):
            return record
        outcome = {"ok": True, "status": "pending", "request_id": record["id"], "record": record}
        if user_quote:
            outcome["quote_refused"] = refused
        return outcome

    quote_check = check_user_quote(requester_sid, user_quote)
    if isinstance(quote_check, str):
        return request_approval(quote_check)
    if not _quote_matches_control(kind, action, target_sid, requester_sid,
                                  _governing_sentence(*quote_check), payload):
        return request_approval("user_quote_mismatch")
    _REFUSED = "_quote_refused_in_transaction"

    def apply(conn):
        # Recheck after acquiring the write lock: a newer message must not inherit an older quote.
        checked = check_user_quote(requester_sid, user_quote, cursor=conn)
        if isinstance(checked, str):
            return {_REFUSED: checked}
        if not _quote_matches_control(kind, action, target_sid, requester_sid,
                                      _governing_sentence(*checked), payload,
                                      cursor=conn):
            return {_REFUSED: "user_quote_mismatch"}
        quote, message = checked
        authority = {"via": "quote", "quote": quote, "message": message}
        raw = _meta_value(conn, _definition_key(kind, target_sid))
        affected_before = _affected_text(kind, action, raw, payload)
        conn.execute("SAVEPOINT session_control_quote")
        result = _manager_apply(kind, action, target_sid, reason=reason, payload=payload,
                                authority=authority, cursor=conn)
        manager_result = result.get("result")
        code = ""
        if isinstance(manager_result, dict) and not manager_result.get("ok", True):
            code = manager_result.get("error_code") or manager_result.get("error") or "apply_failed"
            conn.execute("ROLLBACK TO session_control_quote")
        conn.execute("RELEASE session_control_quote")
        now = _now()
        record = _new_record(kind, action, target_sid, requester_sid, reason=reason, payload=payload,
                             authority=authority, status="failed" if code else "applied", now=now,
                             expires_at=None, affected_text=affected_before, cursor=conn)
        record["resolved_at"], record["error"] = now, code or None
        _bind_continuation(record, result, conn)
        _save_record(record, cursor=conn)
        return {"ok": not bool(code), "status": record["status"], "record": record,
                **({"error_code": code, "error": code} if code else {}), **result}

    try:
        outcome = _db()._execute_write(apply)
    except Exception as exc:
        return {"ok": False, "error_code": "apply_failed", "error": str(exc)}
    if _REFUSED in outcome:
        return request_approval(outcome[_REFUSED])
    return outcome

def resolve_request(request_id: str, decision: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Validate, mutate through the shared manager, and settle in one write transaction.

    The pending check, the manager mutation and the outcome receipt commit together, so a crash
    at any point before commit leaves the request pending with no change applied; there is no
    intermediate ``applying`` state to recover."""
    if decision not in {"approve", "deny"}:
        return None
    db = _db()
    if db is None:
        return None
    key, now = _record_key(request_id), _now()

    def resolve(conn):
        raw = _meta_value(conn, key)
        try:
            record = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("status") != "pending":
            return None
        if float(record.get("expires_at") or 0) <= now:
            record["status"], record["resolved_at"] = "expired", now
            _save_record(record, cursor=conn)
            return None
        record["authority"] = {"via": "button", "user_id": str(user_id)}
        record["resolved_at"] = now
        if decision == "deny":
            record["status"] = "denied"
            _save_record(record, cursor=conn)
            return record

        expected = record.get("target_fingerprint")
        kind, action = record.get("kind"), record.get("action")
        live = _meta_value(conn, _definition_key(kind, record.get("target_session_id", "")))
        if ((kind, action) not in CONTROLS or not isinstance(expected, str)
                or not expected.startswith("v2:") or expected != _definition_fingerprint(kind, live)):
            record["status"], record["error"] = "failed", "target_changed"
            _save_record(record, cursor=conn)
            return record

        # Roll back even a partially saved manager action before persisting a failure receipt.
        conn.execute("SAVEPOINT session_control_apply")
        try:
            result = _manager_apply(kind, action, record["target_session_id"],
                                    reason=record.get("reason") or "session-control",
                                    payload=record.get("payload"), authority=record["authority"], cursor=conn)
            manager_result = result.get("result")
            if isinstance(manager_result, dict) and not manager_result.get("ok", True):
                raise ValueError(manager_result.get("error_code") or manager_result.get("error") or "apply_failed")
            _bind_continuation(record, result, conn)
            record["status"], record["error"] = "applied", None
        except Exception as exc:
            conn.execute("ROLLBACK TO session_control_apply")
            record["status"], record["error"] = "failed", str(exc)
        finally:
            conn.execute("RELEASE session_control_apply")
        _save_record(record, cursor=conn)
        return record

    return db._execute_write(resolve)


def continuation_is_current(request_id: str) -> bool:
    """Discard superseded outbox prompts, including legacy prompts without a definition binding."""
    db = _db()
    if db is None:
        return False

    def validate(conn):
        raw = _meta_value(conn, _record_key(request_id))
        try:
            record = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return False
        if (not isinstance(record, dict) or record.get("status") != "applied"
                or record.get("kind") != "goal" or record.get("action") not in {"resume", "replace"}):
            return False
        if record.get("continuation_discarded"):
            return False
        valid = goal_continuation_is_current({
            "goal_continuation_session_id": record["target_session_id"],
            "goal_continuation_fingerprint": record.get("continuation_fingerprint"),
            "goal_continuation_instance": record.get("continuation_instance"),
        }, cursor=conn)
        if not valid:
            record["continuation_enqueued"] = True
            record["continuation_discarded"] = "target_changed"
            _save_record(record, cursor=conn)
        return bool(valid)

    return db._execute_write(validate)

def fail_request(request_id: str, error: str) -> Optional[Dict[str, Any]]:
    """CAS a pending request to failed so an unroutable target can notify its requester."""
    db = _db()
    if db is None:
        return None
    key, now = _record_key(request_id), _now()

    def _fail(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if record.get("status") != "pending":
            return None
        record["status"], record["error"], record["resolved_at"] = "failed", error, now
        db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
        return record

    return db._execute_write(_fail)


def expire_request(request_id: str) -> Optional[Dict[str, Any]]:
    """Compare-and-set one pending request to expired inside its write transaction."""
    db = _db()
    if db is None:
        return None
    key, now = _record_key(request_id), _now()

    def _expire(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if record.get("status") != "pending" or float(record.get("expires_at") or 0) > now:
            return None
        record["status"], record["resolved_at"] = "expired", now
        db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
        return record

    return db._execute_write(_expire)


def pending_outbox() -> list[Dict[str, Any]]:
    db = _db()
    if db is None:
        return []
    out = []
    now = _now()
    for _key, raw in db.list_meta_prefix(_CONTROL_PREFIX):
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (record.get("status") in {"applied", "denied", "failed", "expired"}
                and not record.get("outbox_done")
                and record.get("resolved_at")
                and now - float(record["resolved_at"]) >= _REQUEST_TTL_SECONDS):
            mark_outbox(record.get("id", ""), "outbox_done")
            continue
        if not record.get("outbox_done") and (
                record.get("status") == "pending"
                or record.get("status") in {"applied", "denied", "failed", "expired"}):
            out.append(record)
    return out


def mark_outbox(request_id: str, flag: str, value: Any = True) -> Optional[Dict[str, Any]]:
    """Atomically set an outbox delivery flag, preserving concurrent flags."""
    db = _db()
    if db is None:
        return None
    key = _record_key(request_id)

    def _mark(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        record[flag] = value
        db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
        return record

    return db._execute_write(_mark)


CONTROLS = {
    ("goal", "pause"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid, cursor=cursor).pause(reason=reason or "session-control"),
    ("goal", "resume"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid, cursor=cursor).resume(),
    ("goal", "clear"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid, cursor=cursor).clear(),
    ("loop", "pause"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid, cursor=cursor).pause(reason=reason or "session-control"),
    ("loop", "resume"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid, cursor=cursor).resume(),
    ("loop", "stop"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid, cursor=cursor).clear(),
    ("goal", "replace"): None,
}

__all__ = [
    "CONTROLS", "resolve_target", "check_user_quote", "quote_matches_control", "apply_control", "request_control",
    "resolve_request", "fail_request", "expire_request", "pending_outbox", "mark_outbox", "continuation_is_current",
]

quote_matches_control = _quote_matches_control
