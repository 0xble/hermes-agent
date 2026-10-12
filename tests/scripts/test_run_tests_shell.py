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
    )
    assert marker.read_text(encoding="utf-8") == "executed", result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert "runner failure propagation canary" in result.stdout + result.stderr


@pytest.mark.platforms("posix", "windows")
def test_shell_runner_preserves_explicit_powershell_cache(tmp_path, real_bash):
    """The isolated parent owns the shell cache, not each test's account home."""
    cache = tmp_path / "shared-cache" / "ModuleAnalysisCache"
    cache.parent.mkdir()
    case = tmp_path / "test_shell_cache_canary.py"
    case.write_text(
        "import os\n"
        "def test_cache_boundary():\n"
        f"    assert os.environ.get('PSModuleAnalysisCachePath') == {str(cache)!r}\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [real_bash, str(root / "scripts/run_tests.sh"), "-j", "1", "--file-retries", "0", str(case)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=180,
        env={**os.environ, "HERMES_PYTHON": sys.executable,
             "PSModuleAnalysisCachePath": str(cache)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
