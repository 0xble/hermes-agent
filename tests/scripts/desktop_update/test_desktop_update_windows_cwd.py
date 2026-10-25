"""Native regression coverage for the Windows Desktop handoff cwd."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest


pytestmark = pytest.mark.platforms("windows")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
WINDOWS_UPDATE_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


def _communicate_with_timeout_diagnostics(
    process: subprocess.Popen[str], timeout: float
) -> str:
    try:
        output, _ = process.communicate(timeout=timeout)
        return output
    except subprocess.TimeoutExpired as exc:
        # Capture the live tree before killing either process; a parent may
        # already have exited while a descendant still holds the pipe open.
        parent_state = process.poll()
        children: list[psutil.Process] = []
        try:
            children = psutil.Process(process.pid).children(recursive=True)
            tree = []
            for child in children:
                try:
                    tree.append((child.pid, child.name(), child.status()))
                except psutil.Error:
                    tree.append((child.pid, "exited", "unavailable"))
        except (psutil.Error, OSError) as error:
            tree = f"unavailable: {error}"

        # On Windows communicate's reader thread owns the buffered stdout lock
        # until EOF. Kill all snapshotted writers before attempting to drain it;
        # never close stdout from this thread if the bounded drain still fails.
        for child in reversed(children):
            try:
                child.kill()
            except (psutil.Error, OSError):
                pass  # It may have exited between the snapshot and kill.
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired as cleanup_exc:
            output = cleanup_exc.output or exc.output
        exc.add_note(
            f"PowerShell poll={parent_state}; descendants={tree}; "
            f"partial stdout={output!r}"
        )
        raise


def _run_cwd_self_test(
    install_root: Path,
    launch_cwd: Path,
    temp_dir: Path,
) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows updater tests require Windows PowerShell."
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(WINDOWS_UPDATE_PS1),
        "-InstallRoot",
        str(install_root),
        "-SelfTestWorkingDirectory",
        "-NoUi",
    ]
    process = subprocess.Popen(
        command,
        cwd=launch_cwd,
        env=env,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = _communicate_with_timeout_diagnostics(process, timeout=60)
    return subprocess.CompletedProcess(command, process.returncode, output)


def test_timeout_diagnostics_kill_pipe_holding_process_tree(tmp_path: Path) -> None:
    # A real grandchild inherits stdout and keeps the pipe open even if its
    # parent is killed. The timeout path must kill it before draining output.
    ready_marker = tmp_path / "child-ready"
    script = (
        "import pathlib, subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print('cwd entered', flush=True); "
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8'); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ready_marker)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    ready_deadline = time.monotonic() + 30
    while not ready_marker.exists():
        if process.poll() is not None:
            output, _ = process.communicate(timeout=5)
            pytest.fail(f"self-test child exited before ready marker: {output!r}")
        if time.monotonic() >= ready_deadline:
            process.kill()
            output, _ = process.communicate(timeout=5)
            pytest.fail(f"self-test child did not become ready: {output!r}")
        time.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _communicate_with_timeout_diagnostics(process, timeout=0.5)
    assert time.monotonic() - started < 8
    note = "\n".join(raised.value.__notes__)
    assert "descendants=[(" in note
    assert "cwd entered" in note
    assert process.poll() is not None


def test_handoff_children_run_from_install_root(tmp_path: Path) -> None:
    install_root = tmp_path / "checkout"
    launch_cwd = tmp_path / "profile-home"
    install_root.mkdir()
    launch_cwd.mkdir()

    result = _run_cwd_self_test(install_root, launch_cwd, tmp_path / "temp")

    assert result.returncode == 0, result.stdout
    assert "WORKING-DIRECTORY SELF-TEST: PASS" in result.stdout


def test_handoff_children_cannot_read_the_handoff_console(tmp_path: Path) -> None:
    # The Desktop starts the hand-off with a visible console. A step that can
    # read it asks its question into captured stdout and waits forever.
    # CREATE_NEW_CONSOLE without redirection gives the hand-off a real console
    # stdin, which is the production shape. The self-test fails when a step
    # can read that console.
    install_root = tmp_path / "checkout"
    install_root.mkdir()
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    output = tmp_path / "self-test.txt"
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows updater tests require Windows PowerShell."
    env = os.environ.copy()
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    command = (
        f"& '{WINDOWS_UPDATE_PS1}' -InstallRoot '{install_root}' "
        f"-SelfTestWorkingDirectory -NoUi *> '{output}'; exit $LASTEXITCODE"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        timeout=60,
        check=False,
    )

    # Windows PowerShell 5.1 `*>` writes UTF-16LE with a BOM.
    report = output.read_text(encoding="utf-16", errors="replace") if output.exists() else ""
    assert result.returncode == 0, report
    assert "WORKING-DIRECTORY SELF-TEST: PASS" in report


def test_handoff_fails_closed_when_install_root_cannot_be_entered(tmp_path: Path) -> None:
    install_root = tmp_path / "missing" / "checkout"
    launch_cwd = tmp_path / "profile-home"
    launch_cwd.mkdir()

    result = _run_cwd_self_test(install_root, launch_cwd, tmp_path / "temp")

    assert result.returncode == 3, result.stdout
    assert "cannot enter the install root" in result.stdout
