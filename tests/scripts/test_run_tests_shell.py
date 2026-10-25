"""The canonical shell must run tests and propagate a failing test's status."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_shell_runner_executes_tests_and_propagates_failure(tmp_path):
    case = tmp_path / "test_runner_canary.py"
    marker = tmp_path / "executed"
    case.write_text(
        "from pathlib import Path\nimport os\n"
        "def test_canary():\n"
        f"    Path({str(marker)!r}).write_text('executed')\n"
        "    assert os.environ['PATHEXT'] == '.COM;.EXE;.BAT;.CMD'\n"
        "    assert False, 'runner failure propagation canary'\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**os.environ, "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "0",
             "PATHEXT": ".COM;.EXE;.BAT;.CMD"},
             check=False,
    )
    assert marker.read_text(encoding="utf-8") == "executed", result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert "runner failure propagation canary" in result.stdout + result.stderr


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("module_path", ["", "runner modules;second module directory"])
def test_shell_runner_preserves_explicit_power_shell_module_path(tmp_path, module_path):
    """An empty value must survive env -i too: PowerShell distinguishes it from unset."""
    case = tmp_path / "test_module_path.py"
    case.write_text(
        "import os\n"
        "def test_module_path():\n"
        f"    assert os.environ['PSModulePath'] == {module_path!r}\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "HERMES_PYTHON": sys.executable,
           "HERMES_TEST_FILE_RETRIES": "0", "PSModulePath": module_path}
    env.pop("__HERMES_ACTIVATED", None)
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.platforms("posix")
def test_shell_runner_preserves_powershell_analysis_cache_path(tmp_path):
    """The isolated runner must retain the parent's explicit PS cache location."""
    case = tmp_path / "test_analysis_cache_path.py"
    cache_path = str(tmp_path / "powershell-cache")
    case.write_text(
        "import os\n"
        "def test_analysis_cache_path():\n"
        f"    assert os.environ['PSModuleAnalysisCachePath'] == {cache_path!r}\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "HERMES_PYTHON": sys.executable,
           "HERMES_TEST_FILE_RETRIES": "0", "PSModuleAnalysisCachePath": cache_path}
    env.pop("__HERMES_ACTIVATED", None)
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
