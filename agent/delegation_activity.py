"""Allowlisted display facts from runtime waits, never model-authored phases."""
from contextlib import contextmanager
import logging
import uuid

logger = logging.getLogger(__name__)

WAIT_LABELS = {
    "process": "Waiting for process",
    "provider_rate_limit": "Provider rate limit",
    "provider_capacity": "Provider capacity",
}
TERMINAL_LABELS = {
    "provider_billing": "Provider billing limit",
    "provider_rate_limit": "Provider rate limit",
}


def terminal_reason(result):
    if not isinstance(result, dict) or not isinstance(result.get("failure_reason"), str):
        return None
    if result.get("failure_reason") == "billing" and result.get("billing_unverified") is False:
        return "provider_billing"
    if result.get("failure_reason") in {"rate_limit", "upstream_rate_limit"}:
        return "provider_rate_limit"
    return None


@contextmanager
def observed_wait(callback, reason):
    """Bracket only an actual blocking wait. Exception/interrupt exits always clear it."""
    if not callable(callback) or not isinstance(reason, str) or reason not in WAIT_LABELS:
        yield
        return
    token = uuid.uuid4().hex
    def emit(active):
        try:
            callback("runtime.wait", reason=reason, wait_id=token, active=active)
        except Exception:
            # The display channel must never change the wait or recovery outcome.
            logger.debug("Runtime wait display callback failed", exc_info=True)
    emit(True)
    try:
        yield
    finally:
        emit(False)
