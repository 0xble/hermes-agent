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
            return False
        if not is_active(record):
            agent._shared_primary_cooldown_record = record
            from agent.shared_primary_cooldown import claim_probe, route_from_record
            probe = claim_probe(route_from_record(record), str(record.get("outage_id") or ""))
            if probe:
                agent._shared_primary_probe_token = probe
                return False
            if probe is False:
                from agent.error_classifier import FailoverReason
                from agent.chat_completion_helpers import try_activate_fallback
                try:
                    reason = FailoverReason(str(record.get("reason") or FailoverReason.rate_limit.value))
                except ValueError:
                    reason = FailoverReason.rate_limit
                return bool(try_activate_fallback(agent, reason, _shared_cooldown_record=record))
            # A concurrent writer may have replaced the record between our read
            # and claim.  A missing/tombstoned record means the primary is clear,
            # but a surviving record still owns the fallback decision.
            current = read_cooldown(route_from_agent(agent))
            if current is None:
                return False
            from agent.error_classifier import FailoverReason
            from agent.chat_completion_helpers import try_activate_fallback
            try:
                reason = FailoverReason(str(current.get("reason") or FailoverReason.rate_limit.value))
            except ValueError:
                reason = FailoverReason.rate_limit
            return bool(try_activate_fallback(agent, reason, _shared_cooldown_record=current))
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
