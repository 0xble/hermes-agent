from unittest.mock import MagicMock


def test_gateway_auto_prune_outside_deletion_window_leaves_throttle_unspent(monkeypatch):
    import gateway.run as gateway_run
    import hermes_cli.config
    import hermes_cli.maintenance_window
    import hermes_state_registry

    monkeypatch.setattr(
        hermes_cli.config,
        "load_config",
        lambda: {"sessions": {"auto_prune": True, "retention_days": 30}},
    )
    monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: False)
    acquire = MagicMock()
    monkeypatch.setattr(hermes_state_registry, "acquire", acquire)

    gateway_run._housekeeping_state_db_maintenance()

    acquire.assert_not_called()
