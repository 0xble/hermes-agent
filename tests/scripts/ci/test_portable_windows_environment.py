"""The hermetic native Windows environment distinguishes missing and empty module paths."""

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ci import portable


pytestmark = pytest.mark.platforms("windows")

# Each cmdlet lives in a built-in module that must autoload from $PSHOME\Modules.
_AUTOLOAD_PROBE = r"""
$ErrorActionPreference = 'Stop'
try { $null = Get-Process -Id $PID; [Console]::WriteLine('Get-Process=OK') }
catch { [Console]::WriteLine('Get-Process=FAIL:' + $_.Exception.Message); exit 21 }
try { $null = Get-CimInstance Win32_OperatingSystem; [Console]::WriteLine('Get-CimInstance=OK') }
catch { [Console]::WriteLine('Get-CimInstance=FAIL:' + $_.Exception.Message); exit 22 }
"""


@pytest.mark.parametrize("module_path", [None, "", r"C:\Runner Modules;C:\Second Module Directory"])
def test_native_windows_environment_preserves_or_defaults_module_path(tmp_path, monkeypatch, module_path):
    if module_path is None:
        monkeypatch.delenv("PSModulePath", raising=False)
    else:
        monkeypatch.setenv("PSModulePath", module_path)

    env = portable.environment(tmp_path / "isolated")

    assert env["PSModulePath"] == (module_path if module_path is not None else "")


@pytest.mark.parametrize("host", ["powershell", "pwsh"])
def test_explicitly_empty_module_path_still_autoloads_builtin_cmdlets(tmp_path, monkeypatch, host):
    """An empty PSModulePath must not stop built-in module autoload in either host.

    Reviewers have predicted the opposite; hosted run 38399052622 disproved it.
    """
    monkeypatch.setenv("PSModulePath", "")
    env = portable.environment(tmp_path / "isolated")
    assert env["PSModulePath"] == ""
    if host == "powershell":
        executable = str(Path(env["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    else:
        executable = shutil.which("pwsh", path=env["PATH"]) or pytest.skip("PowerShell 7 is not installed")

    result = subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _AUTOLOAD_PROBE],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Get-Process=OK" in output and "Get-CimInstance=OK" in output, output
