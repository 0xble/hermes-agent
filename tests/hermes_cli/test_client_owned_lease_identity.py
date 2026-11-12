"""Lease identity survives interpreter redirectors without admitting unrelated clients."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.hermes_cli.test_client_owned_discovery import FIRST_CLIENT
from tests.waiting import wait_until


@pytest.mark.platforms("any")
def test_creator_attaches_through_interpreter_trampoline_but_other_client_waits(tmp_path, monkeypatch):
    from gateway.control_socket import query_gateway_control
    from hermes_cli.gateway_runtime import discover_gateway_endpoint, ensure_gateway_runtime

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tmp_path.chmod(0o700)
    env = {**os.environ, "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path),
           "PYTHONPATH": str(Path.cwd()), "CLIENT_TRAMPOLINE": "1"}
    client = subprocess.Popen([sys.executable, "-c", FIRST_CLIENT], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert wait_until(lambda: (tmp_path / "first-attached").exists() or client.poll() is not None,
                          timeout=70), "creator could not recognize its gateway behind a launcher"
        assert (tmp_path / "first-attached").exists(), client.communicate(timeout=5)
        identity = query_gateway_control(tmp_path, "identify", timeout=2)
        assert identity["pid"] != int((tmp_path / "launcher-pid").read_text())
        assert discover_gateway_endpoint(tmp_path).reason_code == "client_owned"
        verdict = ensure_gateway_runtime(tmp_path, timeout=2)
        assert verdict.reason_code == "client_owned" and verdict.endpoint is None
        assert client.poll() is None
        # Identity material must never escape into status files or logs, even if
        # an operator intentionally gives the profile home searchable permissions.
        token = identity.get("client_lease_token")
        assert isinstance(token, str) and token
        for path in [tmp_path / "gateway_state.json", *list((tmp_path / "logs").glob("*.log"))]:
            assert token not in path.read_text(encoding="utf-8")
    finally:
        client.stdin.close()
        client.stdin = None
        out, err = client.communicate(timeout=15)
        assert client.returncode == 0, (out, err)
