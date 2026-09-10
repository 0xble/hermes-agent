"""Invariant tests for #107427: gateway /update must survive its own restart.

Half A — spawner escapes the gateway cgroup; half B — the watcher does not report
success from the pre-restart ``.update_exit_code`` write alone.
"""

import json
import os
import subprocess
import shutil
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Half A — cgroup escape in _spawn_detached_update
# ---------------------------------------------------------------------------


class TestSpawnDetachedUpdateCgroupEscape:
    """_spawn_detached_update must escape the service cgroup when supervised."""

    def test_escapes_with_systemd_run_when_supervised_and_probe_ok(self, tmp_path, monkeypatch):
        """Supervised + probe OK => argv wrapped in systemd-run, env carries the bus."""
        import gateway.slash_commands as sc

        captured: dict = {}

        class FakePopen:
            def __init__(self, *a, **kw):
                captured["argv"] = a[0] if a else kw.get("args")
                captured["env"] = kw.get("env")
                captured["start_new_session"] = kw.get("start_new_session")
                self.pid = 9999

        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/systemd-run" if x == "systemd-run" else "/usr/bin/setsid" if x == "setsid" else None)
        monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: True)
        monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", lambda e=None: {**(e or {}), "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fake/bus", "XDG_RUNTIME_DIR": "/run/user/1000"})
        monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("INVOCATION_ID", "fake-invocation")

        sc._spawn_detached_update(["hermes"], tmp_path / "out.txt", tmp_path / ".update_exit_code")

        assert captured["argv"][0] == "/usr/bin/systemd-run"
        assert "--scope" in captured["argv"]
        assert "--collect" in captured["argv"]
        assert captured["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/fake/bus"
        assert captured["start_new_session"] is True

    def test_falls_back_to_plain_setsId_when_probe_fails(self, tmp_path, monkeypatch):
        """Unsupervised or probe false => plain setsid spawn, no env override."""
        import gateway.slash_commands as sc

        captured: dict = {}

        class FakePopen:
            def __init__(self, *a, **kw):
                captured["argv"] = a[0] if a else kw.get("args")
                captured["env"] = kw.get("env")
                self.pid = 9999

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
        monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: False)
        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/setsid" if x == "setsid" else None)

        sc._spawn_detached_update(["hermes"], tmp_path / "out.txt", tmp_path / ".update_exit_code")

        assert captured["argv"][0] != "systemd-run"
        assert captured["env"] is None


# Finalization and runtime evidence coverage: test_update_lifecycle_notifications.py.
