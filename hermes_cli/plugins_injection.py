"""Host routing for ``PluginContext.inject_message``: classic CLI REPL, Ink TUI / desktop, messaging
gateway (existing ``session_key`` or a new session at ``origin``)."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Optional

logger = logging.getLogger("hermes_cli.plugins")


def inject_plugin_message(
    ctx: Any, content: str, role: str, *, session_key: Optional[str],
    origin: Optional[Mapping[str, Any]],
    await_dispatch: bool = False, correlation_id: Optional[str] = None,
):
    from hermes_cli.plugin_injection import immediate_result, GatewayInjectionHandle, InjectionDelivery

    def finish(accepted, reason):
        if await_dispatch:
            return immediate_result(accepted, reason, session_key=session_key, correlation_id=correlation_id)
        return accepted

    manager = ctx._manager
    msg = content if role == "user" else f"[{role}] {content}"
    cli = manager._cli_ref
    # An origin names a messaging chat; the local REPL is never that chat.
    if cli is not None and origin is None:
        queue_ = cli._interrupt_queue if getattr(cli, "_agent_running", False) else cli._pending_input
        queue_.put(msg)
        return finish(True, "adopted")
    if session_key and origin is not None:
        logger.warning("inject_message: pass session_key or origin, not both")
        return finish(False, "not_routed")
    # Neither: the live gateway picks the chat (gateway/plugin_keyless_inject.py) from the session this
    # call works for, read here on the caller's own context; no gateway keeps main's refusal.
    keyless = not session_key and origin is None
    if keyless and not manager.has_gateway_message_injector:
        logger.warning("inject_message: gateway mode requires an existing session_key or an origin")
        return finish(False, "not_routed")
    if origin is not None and not isinstance(origin, Mapping):
        logger.warning("inject_message: origin must be a mapping in the SessionSource.to_dict() shape")
        return finish(False, "not_routed")
    if not ctx._gateway_injection_allowed():
        logger.warning("inject_message: gateway injection denied for plugin %s; set "
                       "plugins.entries.%s.allow_gateway_injection: true to allow it",
                       ctx.plugin_id, ctx.plugin_id)
        return finish(False, "not_routed")
    # TUI/desktop host is a different slot. It accepts only when it owns this
    # session_key; a miss falls through so a co-resident messaging gateway
    # still receives its own keys. An exception fails closed — do not also
    # hand the same text to the gateway.
    if session_key and manager.has_tui_message_injector:
        try:
            if manager.inject_tui_message(session_key=session_key, content=msg, plugin_id=ctx.plugin_id):
                return finish(True, "adopted")
        except Exception:
            logger.warning("inject_message: TUI scheduling failed for plugin %s", ctx.plugin_id, exc_info=True)
            if await_dispatch:
                delivery = InjectionDelivery(session_key, correlation_id)
                return GatewayInjectionHandle.resolved(delivery.result(False, "dispatch_error", indeterminate=True))
            return False
    if not manager.has_gateway_message_injector:
        logger.warning("inject_message: no live gateway is available")
        return finish(False, "not_routed")
    # The manager's immutable home is the plugin's profile: the gateway creates an origin's session
    # there or nowhere, and a keyed local session must belong to it (never the ambient HERMES_HOME
    # of whatever thread called us).
    if keyless:
        from gateway.session_context import bound_session_key
        target: dict[str, Any] = {"keyless": True, "calling_session_key": bound_session_key(),
                                  "plugin_home": manager.home_path}
    elif origin is None:
        target = {"session_key": session_key, "plugin_home": manager.home_path}
    else:
        target = {"origin": dict(origin), "plugin_home": manager.home_path}
    if not await_dispatch:
        try:
            return bool(manager.inject_gateway_message(**target, content=msg, plugin_id=ctx.plugin_id))
        except Exception:
            logger.warning("inject_message: gateway scheduling failed for plugin %s", ctx.plugin_id, exc_info=True)
            return False
    try:
        return manager.inject_gateway_message(**target, content=msg, plugin_id=ctx.plugin_id,
                                              await_dispatch=True, correlation_id=correlation_id)
    except Exception:
        # An exception from a host does not prove it failed before scheduling.
        logger.warning("inject_message: gateway dispatch outcome unknown for plugin %s", ctx.plugin_id, exc_info=True)
        delivery = InjectionDelivery(session_key, correlation_id)
        return GatewayInjectionHandle.resolved(delivery.result(False, "dispatch_error", indeterminate=True))
