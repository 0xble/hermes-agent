"""Creation provenance shared by loop and goal controls.

Only trusted creation callers select an actor. Missing legacy provenance never
becomes agent ownership merely because an agent requests a later edit.
"""
from __future__ import annotations


def agent_owned(state) -> bool:
    return state is not None and getattr(state, "created_by", "") == "agent"


def user_stop_condition(state) -> bool:
    owner = getattr(state, "stop_condition_owner", "")
    return owner != "agent" if owner else not agent_owned(state)


def owned_control_state(kind, action, target_sid, requester_sid, *, cursor=None):
    from hermes_cli.goals import load_goal
    from hermes_cli.loops import load_loop

    if target_sid != requester_sid or (kind, action) == ("goal", "clear"):
        return None
    state = (load_goal if kind == "goal" else load_loop)(target_sid, cursor=cursor)
    if not agent_owned(state):
        return None
    if kind == "loop" and action == "stop" and state.until and user_stop_condition(state):
        return None
    return state


def apply_owned_control(kind, action, target_sid, requester_sid, reason, user_quote, commit):
    """Use the same transaction and wake outbox, never borrow another session's ownership."""
    from hermes_cli.goals_autonomy import goal_autonomy_enabled
    from hermes_cli.session_controls import _db

    # Retain the timed native-wake semantics of opted-in autonomous goals.
    if user_quote or (kind == "goal" and goal_autonomy_enabled()):
        return None
    state = owned_control_state(kind, action, target_sid, requester_sid)
    if state is None:
        return None
    if not str(reason or "").strip():
        return {"ok": False, "error_code": "reason_required"}

    def apply(conn):
        live = owned_control_state(kind, action, target_sid, requester_sid, cursor=conn)
        if live is None or live.to_json() != state.to_json():
            return {"ok": False, "error_code": "state_changed"}
        return commit(conn, {"via": "agent", "session_id": requester_sid}, "session_control_owned")

    return _db()._execute_write(apply)


def authenticated_relay_sender(db, relay, goal_session_id, profile, receipts, *, cursor=None):
    """The sender session of a relay row when relay's ledger proves this exact delivery, else None.

    Requires a typed-shaped row (no runtime display_kind, not a synthetic/continuation prompt), a
    Hermes header naming this profile, a ledger receipt with the same sender delivered to this goal
    session at this exact row, and a sender that is a human topic session."""
    import json
    import sqlite3
    from hermes_cli.goals import (
        _USER_TYPED_KINDS, _SYNTHETIC_USER_PREFIXES, _relay_sender,
        _relay_receipt_id, _NON_HUMAN_SESSION_SOURCES,
    )

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
        if cursor is None:
            info = db.get_session(sender[1]) or {}
        else:
            row = cursor.execute("SELECT * FROM sessions WHERE id = ?", (sender[1],)).fetchone()
            info = dict(row) if row is not None else {}
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return None
    if not info or str(info.get("source") or "") in _NON_HUMAN_SESSION_SOURCES:
        return None
    # Delegate children inherit the gateway source (e.g. telegram); the creation marker is the proof.
    # Continuations copy model_config verbatim, so a delegate child's continuation carries it too.
    raw_config = info.get("model_config")
    config = raw_config
    if isinstance(raw_config, str) and raw_config:
        try:
            config = json.loads(raw_config)
        except ValueError:
            return None
    if isinstance(config, dict) and config.get("_delegate_from"):
        return None
    return sender[1]


