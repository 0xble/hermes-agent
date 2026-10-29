"""Forward agent update requests to the gateway that owns the control channel."""

from __future__ import annotations


def cmd_agent_update(args) -> int:
    from gateway.control_socket import query_gateway_control
    from gateway.session_context import get_session_env
    from gateway.status import _same_hermes_home
    from gateway.update_launcher import validate_agent_update_reason
    from hermes_cli import gateway as gw

    try:
        reason = validate_agent_update_reason(getattr(args, "reason", None))
    except ValueError as exc:
        print(f"Error: {exc}")
        return 2
    session_id = get_session_env("HERMES_SESSION_ID", "").strip()
    if not session_id:
        print("Error: update requests require a durable messaging session route")
        return 2
    home = gw.get_hermes_home()
    owner = gw.host_multiplexer_serving()
    payload = {"reason": reason, "session_id": session_id}
    if owner is not None and not _same_hermes_home(owner.home, home):
        # The socket belongs to the launch home, but the session belongs to the caller's profile.
        payload["profile"] = gw._current_profile_name()
    result = query_gateway_control(owner.home if owner is not None else home, "agent-update", payload=payload)
    if not result:
        print("Error: gateway update handoff unavailable")
        return 1
    if not result.get("accepted"):
        print(f"Error: {result.get('error') or 'gateway refused update handoff'}")
        return 1
    print(result["handoff"])
    return 0
