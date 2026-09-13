"""Snapshot cleanup may signal only an unambiguous owned Chromium process tree."""
from unittest.mock import Mock

import pytest

from hermes_cli import browser_connect


@pytest.fixture
def process_boundary(tmp_path, monkeypatch):
    import psutil

    root = tmp_path / "snapshots"
    snapshot = root / "personal"
    personal = tmp_path / "real-browser"
    executable = str(tmp_path / "chrome")
    child = Mock()
    process = Mock(info={"cmdline": []})
    process.exe.return_value = executable
    process.children.return_value = [child]
    monkeypatch.setattr(browser_connect, "chromium_executable", lambda browser: executable)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [process])
    wait = Mock(side_effect=lambda tree, timeout: ([], tree))
    monkeypatch.setattr(psutil, "wait_procs", wait)
    return root, snapshot, personal, executable, process, child, wait


@pytest.mark.parametrize("case", ["duplicate", "single_dash_duplicate", "after_terminator", "gates_after_terminator", "bare", "relative", "sibling", "symlink_escape", "non_string"])
def test_ambiguous_or_nonowned_process_is_never_signaled(process_boundary, case):
    root, snapshot, personal, executable, process, child, wait = process_boundary
    data = [f"--user-data-dir={snapshot}"]
    gates = ["--remote-debugging-port=0", "--profile-directory=Default"]
    if case == "duplicate":
        data.append(f"--user-data-dir={personal}")
    elif case == "single_dash_duplicate":
        data.append(f"-user-data-dir={personal}")
    elif case == "after_terminator":
        data.insert(0, "--")
    elif case == "gates_after_terminator":
        gates.insert(0, "--")
    elif case == "bare":
        data += ["--user-data-dir", str(personal)]
    elif case == "relative":
        data = ["--user-data-dir=snapshots/personal"]
    elif case == "sibling":
        data = [f"--user-data-dir={root}-personal"]
    elif case == "symlink_escape":
        root.mkdir()
        personal.mkdir()
        snapshot.symlink_to(personal, target_is_directory=True)
    elif case == "non_string":
        data.append(None)
    process.info["cmdline"] = [executable, *data, *gates]
    assert browser_connect.stop_snapshot_browser_processes(str(root)) == 0
    process.children.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    child.terminate.assert_not_called()
    child.kill.assert_not_called()
    wait.assert_not_called()


@pytest.mark.parametrize("prefix", ["--user-data-dir=", "-user-data-dir="])
def test_owned_snapshot_cleanup_retains_tree_termination_and_kill_fallback(process_boundary, prefix):
    root, snapshot, _, executable, process, child, wait = process_boundary
    process.info["cmdline"] = [executable, prefix + str(snapshot),
                                "--remote-debugging-port=0", "--profile-directory=Default"]
    assert browser_connect.stop_snapshot_browser_processes(str(root)) == 1
    process.children.assert_called_once_with(recursive=True)
    for owned in (process, child):
        owned.terminate.assert_called_once_with()
        owned.kill.assert_called_once_with()
    assert wait.call_count == 2
