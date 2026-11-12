"""A fresh finite-client home has no update receipt to reconcile at startup."""
from unittest.mock import Mock

from hermes_cli import update_cmd, update_cmd_fleet


def test_absent_update_receipt_does_not_resolve_checkout_identity(monkeypatch):
    # Full source identity walks Git release history, with several bounded Git
    # subprocesses. A missing receipt has no runtime SHA to compare against it.
    identity = Mock(return_value="a" * 40)
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", identity)

    assert update_cmd_fleet._receipt_reports_stale_runtime({}) is False
    identity.assert_not_called()
