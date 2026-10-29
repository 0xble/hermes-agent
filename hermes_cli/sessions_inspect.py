"""Read-only JSON entry points for the existing session inspection tools."""

import json
import sqlite3


def _print_result(result: str) -> int:
    print(result)
    payload = json.loads(result)
    return 0 if payload.get("ok", payload.get("success", False)) else 1


def cmd_session_state(args) -> int:
    from hermes_constants import get_hermes_home, profile_name_for_home
    from tools.session_state_tool import session_state_tool

    selectors = {
        name: getattr(args, name) for name in
        ("chat_id", "thread_id", "session_id", "session_key", "obligation_id", "include_content")
        if getattr(args, name, None) is not None
    }
    profile = profile_name_for_home(get_hermes_home()) or "default"
    return _print_result(session_state_tool({"action": args.state_action, **selectors}, profile=profile))


def cmd_session_search(args) -> int:
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB, format_session_db_unavailable
    from tools.session_search_tool import session_search

    try:
        db = SessionDB(db_path=get_hermes_home() / "state.db", read_only=True)
    except (OSError, sqlite3.Error):
        return _print_result(json.dumps({"success": False, "error": format_session_db_unavailable()}))
    try:
        return _print_result(session_search(
            query=args.query, limit=args.limit, after=args.after, before=args.before, db=db,
        ))
    finally:
        db.close()
