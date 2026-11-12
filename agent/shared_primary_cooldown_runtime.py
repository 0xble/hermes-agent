"""Fresh-agent adoption of durable primary cooldowns, including unavailable state."""

import logging

logger = logging.getLogger(__name__)


def _adopt_shared_primary_cooldown(agent) -> bool:
    """Start a fresh agent on the configured fallback when another process armed the primary route."""
    if not getattr(agent, "_fallback_chain", None):
        return False  # nothing to adopt onto; skip the state-file lock on every turn
    from agent.shared_primary_cooldown import is_active, read_cooldown, route_from_agent
    try:
        record = read_cooldown(route_from_agent(agent))
    except OSError:
        # Unknown durable state is not permission to probe. Use the configured fallback
        # without inventing an outage record, re-arming it, or dropping cached state.
        logger.warning("Shared primary cooldown unavailable; refusing a primary probe", exc_info=True)
        from agent.chat_completion_helpers import try_activate_fallback
        from agent.error_classifier import FailoverReason
        if try_activate_fallback(agent, FailoverReason.cooldown_state_unreadable):
            return True
        # Construction suppresses adoption errors; the turn-start check retries and must
        # refuse the turn if no fallback can be bound, rather than use the primary.
        raise
    try:
        if (isinstance(record, dict)
                and str(record.get("probe_owner") or "")
                and str(record.get("probe_owner") or "") == str(getattr(agent, "_shared_primary_probe_token", "") or "")):
            agent._shared_primary_cooldown_record = record
            return False
        if record is None:
            agent._shared_primary_cooldown_record = None
            agent._shared_primary_probe_token = None
            return False
        if not is_active(record):
            agent._shared_primary_cooldown_record = record
            from agent.shared_primary_cooldown import ProbeClaimKind, ProbeClaimOutcome, claim_probe, route_from_record
            outcome = claim_probe(route_from_record(record), str(record.get("outage_id") or ""))
            if not isinstance(outcome, ProbeClaimOutcome):
                logger.warning("Primary cooldown probe returned an invalid outcome; adopting fallback")
                outcome = ProbeClaimOutcome(ProbeClaimKind.UNAVAILABLE)
            if outcome.kind == ProbeClaimKind.CLAIMED:
                agent._shared_primary_probe_token = outcome.token
                return False
            if outcome.kind in (ProbeClaimKind.OWNED_ELSEWHERE, ProbeClaimKind.STILL_COOLING,
                                ProbeClaimKind.UNAVAILABLE):
                from agent.error_classifier import FailoverReason
                from agent.chat_completion_helpers import try_activate_fallback
                try:
                    reason = FailoverReason(str(record.get("reason") or FailoverReason.rate_limit.value))
                except ValueError:
                    reason = FailoverReason.rate_limit
                return bool(try_activate_fallback(agent, reason, _shared_cooldown_record=record))
            if outcome.kind == ProbeClaimKind.CLEARED:
                agent._shared_primary_cooldown_record = None
                return False
            if outcome.kind == ProbeClaimKind.REPLACED and isinstance(outcome.record, dict):
                record = outcome.record
                agent._shared_primary_cooldown_record = record
                from agent.error_classifier import FailoverReason
                from agent.chat_completion_helpers import try_activate_fallback
                try:
                    reason = FailoverReason(str(record.get("reason") or FailoverReason.rate_limit.value))
                except ValueError:
                    reason = FailoverReason.rate_limit
                return bool(try_activate_fallback(agent, reason, _shared_cooldown_record=record))
            raise AssertionError(f"unhandled probe claim outcome: {outcome.kind!r}")
        from agent.error_classifier import FailoverReason
        try:
            reason = FailoverReason(str(record.get("reason") or FailoverReason.rate_limit.value))
        except ValueError:
            reason = FailoverReason.rate_limit
        from agent.chat_completion_helpers import try_activate_fallback
        return bool(try_activate_fallback(agent, reason, _shared_cooldown_record=record))
    except Exception:
        logger.debug("Shared primary cooldown adoption failed; using normal runtime", exc_info=True)
        return False
