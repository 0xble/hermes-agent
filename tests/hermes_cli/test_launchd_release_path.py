"""A release plist's PATH is a property of the service, not of whoever renders it."""
from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

from hermes_cli import gateway

LAUNCHD_DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _release(root: Path) -> Path:
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(python.stat().st_mode | stat.S_IEXEC)
    (root / "node_modules" / ".bin").mkdir(parents=True)
    return root


@pytest.fixture
def service(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    old, new = _release(home / "releases" / "old"), _release(home / "releases" / "new")
    (home / "current").parent.mkdir(parents=True, exist_ok=True)
    (home / "current").symlink_to(new)
    installed = tmp_path / "LaunchAgents" / "ai.hermes.gateway.plist"
    installed.parent.mkdir()
    user = ["/Users/u/.local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    installed.write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway", "EnvironmentVariables": {
        "PATH": ":".join([str(home / "current/.venv/bin"), str(new / "node_modules/.bin"), *user])}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: installed)
    # The guardian imports the code of the release it is rolling back from.
    monkeypatch.setattr(gateway, "PROJECT_ROOT", new)
    monkeypatch.setattr(gateway, "get_python_path", lambda: str(new / ".venv/bin/python"))
    return home, old, new, user


def _path(release_target: Path) -> list[str]:
    body = gateway.generate_launchd_plist(release_target=release_target).encode()
    return plistlib.loads(body)["EnvironmentVariables"]["PATH"].split(":")


@pytest.mark.platforms("macos")
def test_guardian_rollback_render_keeps_the_installed_user_path(service, monkeypatch):
    home, old, new, user = service
    monkeypatch.setenv("PATH", LAUNCHD_DEFAULT_PATH)

    path = _path(old)

    assert path[:2] == [str(home / "current/.venv/bin"), str(old / "node_modules/.bin")]
    assert "/Users/u/.local/bin" in path  # where the 1Password op CLI lives
    assert path[-len(user):] == user
    assert not any(entry.startswith(str(new)) for entry in path)


@pytest.mark.platforms("macos")
def test_shell_and_launchd_renders_of_one_target_agree(service, monkeypatch):
    home, old, _, user = service
    monkeypatch.setenv("PATH", LAUNCHD_DEFAULT_PATH)
    from_launchd = _path(old)
    monkeypatch.setenv("PATH", ":".join(user))
    assert _path(old) == from_launchd
    # A dir new to the operator's shell is still added, after the installed entries.
    monkeypatch.setenv("PATH", ":".join(["/Users/u/.new/bin", *user]))
    assert _path(old) == [*from_launchd, "/Users/u/.new/bin"]


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("damage", [b"", b"<?xml version='1.0'?><plist><dict><key>Label", b"not a plist"])
def test_unreadable_installed_plist_is_rewritten_not_fatal(service, monkeypatch, damage):
    home, old, _, _ = service
    gateway.get_launchd_plist_path().write_bytes(damage)
    monkeypatch.setenv("PATH", LAUNCHD_DEFAULT_PATH)
    assert _path(old)[0] == str(home / "current/.venv/bin")
    assert gateway.launchd_plist_is_current(release_target=old) is False
