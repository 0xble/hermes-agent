"""CLI lifecycle stops pass only the unfinished outcome to delegation consumers."""

from hermes_cli.cli_shutdown import _interrupt_async_delegations
from tools.process_registry_notifications import format_process_notification


def test_cli_cleanup_delegation_reason_is_outcome_only(monkeypatch):
    reasons = []
    monkeypatch.setattr("tools.async_delegation.interrupt_all", lambda *, reason: reasons.append(reason))

    _interrupt_async_delegations()

    (reason,) = reasons
    assert reason == "stopped before finishing"
    text = format_process_notification({
        "type": "async_delegation", "delegation_id": "deleg_cli_stop",
        "status": "interrupted", "error": reason,
    })
    assert text is not None
    assert "stopped before finishing" in text
    for cause in ("restart", "shutdown", "killed"):
        assert cause not in text.lower()
