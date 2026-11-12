"""Upgrade sandboxes must never rendezvous with the host or a sibling gateway."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("posix")


def test_gateway_rendezvous_is_private_to_each_sandbox(tmp_path, monkeypatch):
    from gateway.status import _get_lock_dir
    from tests.e2e.core.upgrade._helpers import isolated_env

    host = tmp_path / "not-a-sandbox"
    monkeypatch.setenv("XDG_STATE_HOME", str(host / "state"))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(host / "locks"))
    roots = [tmp_path / "first", tmp_path / "second"]
    namespaces = []
    for root in roots:
        env = isolated_env(root)
        with monkeypatch.context() as child:
            for key in ("HOME", "HERMES_HOME", "XDG_STATE_HOME", "HERMES_GATEWAY_LOCK_DIR"):
                child.setenv(key, env[key])
            namespace = _get_lock_dir()
        assert namespace.is_relative_to(root)
        assert namespace.is_dir()
        assert Path(env["HOME"]).is_relative_to(root)
        assert Path(env["HERMES_HOME"]).is_relative_to(root)
        namespaces.append(namespace)
    assert namespaces[0] != namespaces[1]
    assert not host.exists(), "sandbox setup touched the inherited host namespace"
