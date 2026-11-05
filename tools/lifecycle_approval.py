"""Fork policy for agent-initiated lifecycle requests, not operator or scheduled updates."""
from collections.abc import Callable
from datetime import datetime
import time
from zoneinfo import ZoneInfo


_AGENT_LIFECYCLE_TOOLS = frozenset({"request_update", "request_restart"})


def maintenance_window_open(*, clock: Callable[[], float] | None = None) -> bool:
    """Use Pacific civil time, including DST; the upper bound is exclusive."""
    now = datetime.fromtimestamp((clock or time.time)(), ZoneInfo("America/Los_Angeles"))
    return 2 <= now.hour < 6


def lifecycle_approval(name: str, args: dict) -> dict | None:
    """None permits the existing handler unchanged; a result refuses without side effects."""
    if name not in _AGENT_LIFECYCLE_TOOLS or maintenance_window_open():
        return None
    from tools.approval_prompt import ApprovalUnavailable, request_elicitation_consent

    action = "update and restart" if name == "request_update" else "restart"
    message = f"Approve Hermes {action}? Reason: {args.get('reason') or '(not supplied)'}"
    if name == "request_update" and args.get("branch") is not None:
        message += f"; branch: {args['branch']}"
    description = ("Agent-initiated request outside 2–6 AM America/Los_Angeles. "
                   "Approve runs it now; Deny or no response does nothing.")
    try:
        decision = request_elicitation_consent(
            message, description, surface="agent-lifecycle", title="Approve Hermes lifecycle request?")
    except ApprovalUnavailable:
        status = "approval_unavailable"
        detail = "No approval card could be delivered"
    else:
        if decision == "accept":
            return None
        status = "denied" if decision == "decline" else "timeout"
        detail = "Request denied" if status == "denied" else "No approval received (timeout or cancellation)"
    return {"success": False, "status": status, "error_code": status,
            "error": f"{detail}; nothing was updated or restarted."}
