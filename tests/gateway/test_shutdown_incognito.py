"""Incognito shutdown cleanup must stay best-effort and retain its gateway log contract."""

import logging

import pytest

from gateway.run_shutdown_incognito import cleanup_incognito_private_dirs


@pytest.mark.parametrize("removed", [0, 2])
def test_cleanup_logs_only_removed_intervals(monkeypatch, caplog, removed):
    monkeypatch.setattr("agent.incognito_private_dir.discard_owned_private_dirs", lambda: removed)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        cleanup_incognito_private_dirs()

    assert caplog.messages == (
        ["Incognito shutdown cleanup removed 2 private directories"] if removed else []
    )


def test_cleanup_failure_does_not_abort_shutdown(monkeypatch):
    def fail():
        raise OSError("private directory unavailable")

    monkeypatch.setattr("agent.incognito_private_dir.discard_owned_private_dirs", fail)

    cleanup_incognito_private_dirs()
