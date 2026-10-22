"""The updater must not send launchd bootout after a gateway accepts a bounded wait."""

import os
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("accepted", [True, False])
def test_reload_hands_off_to_gateway_before_bootout(tmp_path, monkeypatch, accepted):
    from gateway import control_socket
    from hermes_cli import gateway, gateway_launchd

    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: os.getpid())
    query = Mock(return_value={"accepted": accepted, "pid": os.getpid()})
    monkeypatch.setattr(control_socket, "query_gateway_control", query)
    helper = Mock(return_value=True)
    monkeypatch.setattr(gateway_launchd, "_spawn_deferred_launchd_reload", helper)
    if accepted:
        assert gateway_launchd._reload_installed_launchd_plist(tmp_path / "gateway.plist") == "deferred"
    else:
        with pytest.raises(RuntimeError, match="no bootout sent"):
            gateway_launchd._reload_installed_launchd_plist(tmp_path / "gateway.plist")
    query.assert_called_once_with(tmp_path, "reload-service", timeout=10.0)
    helper.assert_not_called()


def test_restart_wait_defaults_are_900_seconds():
    from gateway.restart import (
        DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
        DEFAULT_GATEWAY_RESTART_DELEGATION_TIMEOUT,
    )

    assert DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT == 900
    assert DEFAULT_GATEWAY_RESTART_DELEGATION_TIMEOUT == 900
