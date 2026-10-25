"""Fixtures shared by state.db maintenance tests."""

import pytest


@pytest.fixture(autouse=True)
def _open_auto_maintenance_window(monkeypatch):
    """Keep state.db auto-maintenance tests deterministic regardless of the wall clock."""
    import hermes_cli.maintenance_window

    monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: True)
