"""Native peer-wait commands through the current session's existing goal RPC."""
from __future__ import annotations

import asyncio
import json
import shlex
import sys

USAGE = ('Usage: /goal peer wait|resolve <goal-created-at> <hermes:profile/session> '
         '<work-ref> "<requested result|recorded proof>"')


def peer_request(arg):
    tokens = shlex.split(arg)
    if not tokens or tokens == ["list"]:
        return "state", [], {}
    if len(tokens) != 5 or tokens[0] not in {"wait", "resolve"}:
        raise ValueError(USAGE)
    verb, instance, peer, ref, text = tokens
    return ("wait_on_peer" if verb == "wait" else "resolve_peer",
            [peer, ref, text], {"expected_instance": float(instance)})


def peer_list(state):
    if state is None:
        return "No goal."
    return (f"Goal instance: {state.created_at}\n" +
            ("\n".join(f"Pending peer: {row['peer']} [{row['ref']}] {row['result']}"
                       for row in state.pending_peers) or "No pending peer work."))


def dispatch_peer_command(mgr, arg):
    from hermes_cli.goal_command import GoalCommandResult

    call, args, kwargs = peer_request(arg)
    if call == "state":
        return GoalCommandResult(peer_list(mgr.state))
    result = getattr(mgr, call)(*args, **kwargs)
    if not result or not result.get("ok"):
        return GoalCommandResult((result or {}).get("error", "peer change was not saved"), error=True)
    return GoalCommandResult("Peer work registered." if call == "wait_on_peer" else "Peer result verified and resolved.")


async def run_command(arg):
    """Attach only. Never start a service, choose another parent, or write SessionDB here."""
    from gateway.session_context import bound_session_env
    from hermes_constants import get_hermes_home
    from hermes_cli.gateway_client import connect_gateway
    from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint, gateway_profile_home
    from hermes_cli.goals import GoalState

    sid = bound_session_env("HERMES_SESSION_ID", "")
    if not sid:
        raise ValueError("peer controls require the originating parent's current session")
    call, args, kwargs = peer_request(arg)
    discovered = await asyncio.to_thread(discover_gateway_endpoint, gateway_profile_home(get_hermes_home()))
    if not attachable(discovered):
        raise ValueError("the owning gateway is not ready, no change was submitted")
    async with connect_gateway(endpoint=discovered.endpoint) as client:
        await client.rpc("session.resume", session_id=sid)
        reply = await client.rpc("session.automation", session_id=sid, kind="goal", call=call,
                                 args=args, kwargs=kwargs)
    if call == "state" and not reply.get("error"):
        reply["output"] = peer_list(GoalState.from_json(reply["state"]) if reply.get("state") else None)
    return reply


def main():
    try:
        result = asyncio.run(run_command(shlex.join(sys.argv[1:])))
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("error") or (result.get("value") or {}).get("ok") is False else 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
