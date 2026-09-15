"""Snapshot cleanup binds wrapper launches to a specific installed binary."""
import os
from unittest.mock import Mock

import pytest

from hermes_cli import browser_connect


@pytest.fixture
def installed_package(tmp_path, monkeypatch):
    import psutil

    package = tmp_path / "package"
    package.mkdir()
    launcher = package / "google-chrome"
    launcher.write_text('#!/bin/sh\nexec "$(dirname "$0")/chrome" "$@"\n')
    launcher.chmod(0o755)
    binary = package / "chrome"
    binary.write_bytes(b"installed browser fixture")
    binary.chmod(0o755)
    root = tmp_path / "snapshots"
    profile = root / "personal"
    alias = tmp_path / "google-chrome-stable"
    alias.symlink_to(launcher)
    # Substitute installation data, not host OS behavior. Files and symlinks are real;
    # only process enumeration/signals are replaced so no live browser is touched.
    monkeypatch.setattr(browser_connect, "_LINUX_BROWSER_LAUNCHER_BINARIES", {
        "chrome": {str(launcher): str(binary)},
    }, raising=False)
    monkeypatch.setattr(browser_connect, "chromium_executable",
                        lambda browser: str(alias) if browser == "chrome" else None)
    child = Mock()
    process = Mock(info={"cmdline": [str(launcher), f"--user-data-dir={profile}",
                                    "--remote-debugging-port=0", "--profile-directory=Default"]})
    process.exe.return_value = str(binary)
    process.children.return_value = [child]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [process])
    wait = Mock(side_effect=lambda tree, timeout: ([], tree))
    monkeypatch.setattr(psutil, "wait_procs", wait)
    return root, launcher, binary, alias, process, child, wait


@pytest.mark.parametrize("via_alias", [False, True])
def test_owned_installed_wrapper_binary_reaches_tree_cleanup(installed_package, monkeypatch, via_alias):
    root, launcher, _, alias, process, child, wait = installed_package
    monkeypatch.setattr(browser_connect, "chromium_executable",
                        lambda browser: str(alias if via_alias else launcher) if browser == "chrome" else None)
    assert browser_connect.stop_snapshot_browser_processes(str(root)) == 1
    process.children.assert_called_once_with(recursive=True)
    for owned in (process, child):
        owned.terminate.assert_called_once_with()
        owned.kill.assert_called_once_with()
    assert wait.call_count == 2


@pytest.mark.parametrize("case", [
    "forged_basename", "other_package", "missing_binary", "nonexecutable_binary",
    "binary_symlink_escape", "missing_launcher", "nonexecutable_launcher", "unknown_wrapper",
    "different_browser", "directory_binary", "duplicate_profile", "profile_escape", "renderer",
])
def test_unbound_or_nonowned_wrapper_process_is_never_signaled(installed_package, monkeypatch, case):
    root, launcher, binary, _, process, child, wait = installed_package
    unrelated = root.parent / "unrelated"
    unrelated.mkdir()
    other = unrelated / "chrome"
    other.write_bytes(b"unrelated executable")
    other.chmod(0o755)
    def nonexecutable(path):
        path.chmod(0o644)
        if os.access(path, os.X_OK):
            pytest.skip("Host does not enforce executable file permission bits")

    def redirected_binary():
        binary.unlink()
        binary.symlink_to(other)
        process.exe.return_value = str(other)

    def different_package():
        other_browser = unrelated / "brave"
        other_browser.write_bytes(b"another installed browser")
        other_browser.chmod(0o755)
        process.exe.return_value = str(other_browser)

    def directory_binary():
        binary.unlink()
        binary.mkdir()

    actions = {
        "forged_basename": lambda: setattr(process.exe, "return_value", str(other)),
        "other_package": different_package,
        "missing_binary": binary.unlink,
        "nonexecutable_binary": lambda: nonexecutable(binary),
        "binary_symlink_escape": redirected_binary,
        "missing_launcher": launcher.unlink,
        "nonexecutable_launcher": lambda: nonexecutable(launcher),
        "unknown_wrapper": lambda: monkeypatch.setattr(
            browser_connect, "chromium_executable", lambda browser: str(unrelated / "google-chrome")),
        "different_browser": lambda: monkeypatch.setattr(
            browser_connect, "chromium_executable", lambda browser: str(launcher) if browser == "brave" else None),
        "directory_binary": directory_binary,
        "duplicate_profile": lambda: process.info["cmdline"].append(f"--user-data-dir={unrelated}"),
        "profile_escape": lambda: process.info["cmdline"].__setitem__(1, f"--user-data-dir={unrelated}"),
        "renderer": lambda: process.info["cmdline"].append("--type=renderer"),
    }
    actions[case]()
    assert browser_connect.stop_snapshot_browser_processes(str(root)) == 0
    process.children.assert_not_called()
    for candidate in (process, child):
        candidate.terminate.assert_not_called()
        candidate.kill.assert_not_called()
    wait.assert_not_called()
