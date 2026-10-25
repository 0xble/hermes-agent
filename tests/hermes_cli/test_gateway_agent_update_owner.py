"""Agent update handoffs use the verified owner's socket, not a satellite's home."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from gateway import host_attach, host_rendezvous
from gateway.control_socket import GatewayControlServer, resolve_client_socket_path
from hermes_cli.gateway import _cmd_update


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("profile", ("other", "default"))
def test_agent_update_reaches_owner_socket_with_requesting_profile(
    tmp_path, monkeypatch, capsys, profile,
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    satellite = root / "profiles" / "other"
    satellite.mkdir(parents=True)
    (satellite / "config.yaml").write_text("{}\n", encoding="utf-8")
    home = satellite if profile == "other" else root
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_ID", "requesting-session")
    monkeypatch.setattr("hermes_constants._default_hermes_root_memo", None)
    monkeypatch.setattr(host_rendezvous, "host_state_dir", lambda: tmp_path / "rendezvous")
    monkeypatch.setattr(host_attach, "_cached_probe", None)
    received = []

    def accept(payload):
        received.append(payload)
        return {"accepted": True, "handoff": "owner accepted"}

    async def scenario():
        server = GatewayControlServer(root, verb_handlers={
            "identify": lambda: {"pid": os.getpid(), "hermes_home": str(root),
                                 "served_profiles": ["default", "other"]},
            "agent-update": accept,
        })
        assert await server.start()
        assert host_rendezvous.publish_record(host_rendezvous.ROLE_GATEWAY, home=str(root))
        try:
            assert resolve_client_socket_path(root) is not None
            assert resolve_client_socket_path(satellite) is None
            return await asyncio.to_thread(_cmd_update, SimpleNamespace(reason="Apply owner routing fix"))
        finally:
            await server.stop()
            host_attach.invalidate_host_gateway_cache()

    result = asyncio.run(scenario())
    assert result == 0, capsys.readouterr().out
    assert capsys.readouterr().out.strip() == "owner accepted"
    expected = {"reason": "Apply owner routing fix", "session_id": "requesting-session"}
    if profile != "default":
        expected["profile"] = profile
    assert received == [expected]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("profile", ("other", "default"))
def test_agent_update_without_owner_uses_current_home(tmp_path, monkeypatch, profile):
    root = tmp_path / "root"
    home = root / "profiles" / profile if profile != "default" else root
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_ID", "local-session")
    monkeypatch.setattr("hermes_constants._default_hermes_root_memo", None)
    monkeypatch.setattr(host_rendezvous, "host_state_dir", lambda: tmp_path / "rendezvous")
    monkeypatch.setattr(host_attach, "_cached_probe", None)
    received = []

    def accept(payload):
        received.append(payload)
        return {"accepted": True, "handoff": "local accepted"}

    async def scenario():
        server = GatewayControlServer(home, verb_handlers={"agent-update": accept})
        assert await server.start()
        try:
            return await asyncio.to_thread(_cmd_update, SimpleNamespace(reason="Apply local fix"))
        finally:
            await server.stop()
            host_attach.invalidate_host_gateway_cache()

    assert asyncio.run(scenario()) == 0
    assert received == [{"reason": "Apply local fix", "session_id": "local-session"}]
