"""The recorded cause of a turn interrupted before its first model call, for its reply and replay.

August to October 2026, every "interrupted before processing started (likely by a recent /stop)"
reply in the owner's chat came from a gateway restart, a watchdog or another system stop, never a
/stop. Only a Stop says /stop; a restart's unstarted native turn is replayed instead of answered
(``session_settlement_recovery.requeue_shutdown_unstarted``)."""


def is_owner_shutdown_interrupt(agent_result: dict) -> bool:
    """The turn was interrupted by this gateway's own restart/shutdown stop (``stop()`` records these
    exact reasons, and a user Stop already pending keeps its own), not by a person."""
    from gateway.run import _INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN
    return agent_result.get("interrupt_message") in (_INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN)


def interrupted_before_start_key(agent_result: dict) -> str:
    """The catalog key naming the cause. The messaging Stop is a bare hard interrupt (no message) or
    the classic "Stop requested"; a restart, a watchdog, an eviction or a follow-up message is not one."""
    from gateway.run import _INTERRUPT_REASON_STOP
    if is_owner_shutdown_interrupt(agent_result):
        return "gateway.errors.interrupted_before_start_restart"
    if not agent_result.get("interrupt_source") and agent_result.get("interrupt_message") in (None, "", _INTERRUPT_REASON_STOP):
        return "gateway.errors.interrupted_before_start"
    return "gateway.errors.interrupted_before_start_other"
