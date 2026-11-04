"""Background launches on environment-backed backends run in the requested cwd."""

import os
import shutil
from unittest.mock import MagicMock, patch
import time

import pytest

from tools.environments.local import LocalEnvironment
from tools.environments.local_env_policy import _HERMES_PROVIDER_ENV_FORCE_PREFIX
from tools.process_registry import ProcessRegistry


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_spawn_via_env_runs_command_in_requested_cwd(tmp_path):
    default_dir = tmp_path / "env-default"
    workdir = tmp_path / "requested"
    default_dir.mkdir()
    workdir.mkdir()
    env = LocalEnvironment(cwd=str(default_dir), timeout=30)
    registry = ProcessRegistry()
    try:
        session = registry.spawn_via_env(env, "pwd -P", cwd=str(workdir), task_id="bg-cwd")
        deadline = time.monotonic() + 30
        while not session.exited and time.monotonic() < deadline:
            time.sleep(0.2)

        assert session.exited and session.exit_code == 0, session.output_buffer
        assert session.output_buffer.strip() == str(workdir.resolve())
    finally:
        registry.kill_all()
        env.cleanup()


def test_spawn_local_strips_blocked_vars_from_background_env(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr("tools.process_registry._SYSTEMD_SCOPE_AVAILABLE", False)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        proc = MagicMock()
        proc.pid = 4321
        proc.stdout = iter([])
        proc.stdin = MagicMock()
        proc.poll.return_value = None
        return proc

    fake_thread = MagicMock()

    with patch.dict(os.environ, {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/user",
        "HERMES_HOME": str(tmp_path),
        "USER": "tester",
        "TELEGRAM_BOT_TOKEN": "bot-secret",
        "FIRECRAWL_API_KEY": "fc-secret",
    }, clear=True), \
        patch("tools.process_registry._find_shell", return_value="/bin/bash"), \
        patch("subprocess.Popen", side_effect=fake_popen), \
        patch("threading.Thread", return_value=fake_thread), \
        patch.object(registry, "_write_checkpoint"):
        registry.spawn_local(
            "echo hello",
            cwd="/tmp",
            env_vars={
                "MY_CUSTOM_VAR": "keep-me",
                "TELEGRAM_BOT_TOKEN": "drop-me",
                f"{_HERMES_PROVIDER_ENV_FORCE_PREFIX}TELEGRAM_BOT_TOKEN": "forced-bot-token",
            },
        )

    env = captured["env"]
    assert env["MY_CUSTOM_VAR"] == "keep-me"
    assert env["TELEGRAM_BOT_TOKEN"] == "forced-bot-token"
    assert "FIRECRAWL_API_KEY" not in env
    assert f"{_HERMES_PROVIDER_ENV_FORCE_PREFIX}TELEGRAM_BOT_TOKEN" not in env
    assert env["PYTHONUNBUFFERED"] == "1"
