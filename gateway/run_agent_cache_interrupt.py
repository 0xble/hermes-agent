"""Interrupt-time plugin notification, separate from slot invalidation."""

from logging import Logger

def notify_interrupted_plugins(running_agent, source, session_key, interrupt_reason, invalidation_reason, logger: Logger):
    from gateway.run import _AGENT_PENDING_SENTINEL
    if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
        # Plugins holding a per-turn external resource (an outbound RPC blocked on a tool result
        # the loop will never consume) learn the turn is gone. Fires for /stop and the /new
        # running-agent fast path; the pending-sentinel /stop has no in-flight work, so it stays
        # silent. Dispatch failures are swallowed so a misbehaving plugin cannot break an interrupt.
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook

            _invoke_hook(
                "agent_loop_stopped",
                session_key=session_key,
                platform=source.platform.value if source.platform else "",
                reason=interrupt_reason,
                invalidation_reason=invalidation_reason,
            )
        except Exception:
            logger.error("agent_loop_stopped hook dispatch failed", exc_info=True)
