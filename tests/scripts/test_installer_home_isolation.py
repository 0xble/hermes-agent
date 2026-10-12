"""Installer tests and their children own their machine-level destinations."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_installer_machine_home_covers_python_account_and_child_paths(tmp_path_factory):
    home = Path.home()
    assert home == Path(os.environ["HOME"])
    assert home.is_relative_to(tmp_path_factory.getbasetemp())
    if sys.platform == "darwin":
        from hermes_cli.gateway import get_launchd_plist_path

        assert get_launchd_plist_path().is_relative_to(home / "Library" / "LaunchAgents")
    result = subprocess.run(
        [sys.executable, "-c", "\n".join([
            "import json, pathlib",
            "home = pathlib.Path.home()",
            "node = home / '.local/bin/node'",
            "node.parent.mkdir(parents=True)",
            "node.write_text('fixture', encoding='utf-8')",
            "print(json.dumps(str(node)))",
        ])],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    target = Path(json.loads(result.stdout))
    assert target == home / ".local" / "bin" / "node"
    assert target.read_text(encoding="utf-8-sig") == "fixture"


@pytest.mark.platforms("windows")
def test_installer_machine_home_keeps_the_powershell_module_cache_shared():
    """Installer destinations move into the per-test home; Windows PowerShell's
    module-analysis cache must not, or every child re-scans every module."""
    home = Path.home()
    assert Path(os.environ["LOCALAPPDATA"]).is_relative_to(home)
    cache = Path(os.environ["PSModuleAnalysisCachePath"])
    assert cache.name == "ModuleAnalysisCache"
    assert cache.is_absolute()
    assert not cache.is_relative_to(home)
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(shell), "-NoProfile", "-Command", "\n".join([
            "$watch = [Diagnostics.Stopwatch]::StartNew()",
            "$process = Get-CimInstance Win32_Process -Filter \"ProcessId=$PID\"",
            "@{cache=$env:PSModuleAnalysisCachePath; local=$env:LOCALAPPDATA; "
            "profile=$env:USERPROFILE; process=$process.ProcessId; "
            "systemdrive=$env:SystemDrive; elapsed=$watch.Elapsed.TotalSeconds} | ConvertTo-Json -Compress",
        ])],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
    )
    observed = json.loads(result.stdout)
    assert Path(observed["cache"]) == cache
    assert Path(observed["local"]).is_relative_to(home)
    assert Path(observed["profile"]) == home
    assert observed["process"] > 0
