"""Conservative continuation grants at a durable child checkpoint (no replay)."""
from copy import deepcopy
import json
import logging

logger = logging.getLogger(__name__)


def unresolved_tool_result(content):
    """Recognize real cancellation and orphan receipts, not successful tool prose."""
    from agent.replay_cleanup import is_interrupted_tool_result
    if not isinstance(content, str):
        return False
    if is_interrupted_tool_result(content) or content.startswith(("[Tool execution cancelled", "[Orphan recovery:")):
        return True
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and (
        payload.get("error_type") == "tool_interrupted"
        or str(payload.get("error") or "").startswith("Tool execution cancelled")
    )


def _signature(messages):
    return [(m.get("role"), m.get("content") or "", m.get("tool_calls") or None,
             m.get("tool_call_id") or None) for m in messages if m.get("role") != "system"]


def checkpoint_child_resume(child, result, entry, *, child_task_id=None):
    from tools.delegate_tool import _resume_history_is_safe, _refresh_resumable_launch_metadata
    if entry.get("status") in {"completed", "budget_exhausted", "interrupted"}:
        db = getattr(child, "_session_db", None)
        named_child = getattr(child, "_delegation_named_type", None) is not None
        if named_child:
            entry["resume_available"] = False
        safe_history = _resume_history_is_safe((result or {}).get("messages"))
        try:
            from tools.process_registry import process_registry
            owners = set(getattr(child, "_process_owner_task_ids", ()) or ())
            owners.update((child_task_id, getattr(child, "_current_task_id", None), getattr(child, "_subagent_id", None)))
            if process_registry.unresolved_owned_processes(
                    owner for owner in owners if isinstance(owner, str) and owner):
                safe_history = False
                entry["resume_blocked_reason"] = "unreconciled_background_processes"
        except Exception:
            safe_history = False
            entry["resume_blocked_reason"] = "background_process_registry_unavailable"
        if entry.get("orphaned_processes") or entry.get("unread_completions"):
            safe_history = False
            entry["resume_blocked_reason"] = "unreconciled_background_processes"
        if getattr(child, "_delegation_user_stopped", False) is True:
            safe_history = False
            entry["resume_blocked_reason"] = "user_stopped_requires_explicit_authorization"
        if db is not None and callable(getattr(type(db), "get_messages_as_conversation", None)):
            # Actual durable transcript, not merely the in-memory successful result.
            durable = db.get_messages_as_conversation(child.session_id, repair_alternation=False)
            safe_history = (safe_history and bool(durable) and _resume_history_is_safe(durable)
                            and _signature(durable) == _signature((result or {}).get("messages") or []))
        if not safe_history:
            entry.setdefault("resume_blocked_reason", "unresolved_tool_effects_or_uncheckpointed_history")
            if db is not None:
                try:
                    db.patch_session_model_config(child.session_id, {
                        "_delegation_completed": False, "_delegation_outcome": entry["status"],
                        "_delegation_user_stopped": getattr(child, "_delegation_user_stopped", False) is True,
                        "_delegation_resume_blocked_reason": entry["resume_blocked_reason"],
                    })
                except Exception:
                    logger.exception("Could not persist the interrupted checkpoint blocker")
                    entry["resume_error"] = "durable continuation blocker could not be persisted"
        if db is not None and safe_history:
            try:
                launch_metadata = deepcopy(getattr(child, "_delegation_launch_metadata", None))
                if isinstance(launch_metadata, dict):
                    launch_metadata = _refresh_resumable_launch_metadata(child, launch_metadata)
                model_config_patch = {
                    "_delegation_completed": True,
                    "_delegation_user_stopped": False,
                    "_delegation_resume_blocked_reason": None,
                    "_delegation_outcome": entry["status"],
                    "_delegation_resume_claimed_at": None,
                    "_delegation_active_route": {
                        "provider": getattr(child, "provider", None),
                        "model": getattr(child, "model", None),
                    },
                }
                if isinstance(launch_metadata, dict):
                    model_config_patch["_delegation_launch"] = launch_metadata
                db.patch_session_model_config(
                    getattr(child, "session_id", ""), model_config_patch,
                )
                if named_child:
                    entry["resume_available"] = True
                    entry["resume_session_id"] = child.session_id
            except Exception as exc:
                logger.warning("Could not mark delegated child resumable: %s", exc, exc_info=True)
                entry["resume_error"] = "durable continuation marker could not be persisted"
