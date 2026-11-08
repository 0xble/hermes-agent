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
        cwd=tmp_path, capture_output=True, text=True, timeout=180, env=env, check=False,
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
        cwd=tmp_path, capture_output=True, text=True, timeout=180, env=env, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("live_guard", [False, True])
def test_explicit_interpreter_keeps_pythonpath_across_clean_environment(tmp_path, live_guard):
    """Nix's HERMES_PYTHON finds its editable environment through PYTHONPATH."""
    modules = tmp_path / "nix site-packages"
    modules.mkdir()
    (modules / "runner_nix_dependency.py").write_text("VALUE = 'inherited'\n", encoding="utf-8")
    account = tmp_path / "account"
    account.mkdir()
    if live_guard:
        (account / ".hermes").mkdir()
        (account / ".hermes" / "pytest_live_guard.py").write_text("", encoding="utf-8")
    case = tmp_path / "test_nix_dependency.py"
    case.write_text(
        "from runner_nix_dependency import VALUE\n"
        "def test_dependency():\n"
        "    assert VALUE == 'inherited'\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "HERMES_PYTHON": sys.executable, "PYTHONPATH": str(modules),
           "HERMES_TEST_FILE_RETRIES": "0", "HOME": str(account)}
    env.pop("__HERMES_ACTIVATED", None)
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, env=env, check=False,
    )
    assert "using HERMES_PYTHON" in result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("opt_in", [False, True])
def test_shell_runner_forwards_only_explicit_vault_fixture_paths(tmp_path, opt_in):
    """Companion opt-in reaches pytest without opening the environment allowlist."""
    paths = {
        "VAULT_BROWSER_FIXTURE_MODULES": str(tmp_path / "native modules"),
        "VAULT_BROWSER_FIXTURE_SOURCE": str(tmp_path / "companion source"),
    }
    expected = paths if opt_in else dict.fromkeys(paths)
    case = tmp_path / "test_vault_fixture_environment.py"
    case.write_text(
        "import os\n"
        "def test_fixture_environment():\n"
        f"    expected = {expected!r}\n"
        "    assert {key: os.environ.get(key) for key in expected} == expected\n"
        "    assert 'VAULT_BROWSER_FIXTURE_UNRELATED' not in os.environ\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "HERMES_PYTHON": sys.executable,
           "HERMES_TEST_FILE_RETRIES": "0", "OPENAI_API_KEY": "synthetic-canary",
           "VAULT_BROWSER_FIXTURE_UNRELATED": "synthetic-canary"}
    env.pop("__HERMES_ACTIVATED", None)
    for key in paths:
        env.pop(key, None)
    if opt_in:
        env.update(paths)
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, env=env, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
