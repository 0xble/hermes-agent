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


@pytest.mark.platforms("posix")
def test_shell_runner_preserves_only_explicit_download_caches(tmp_path):
    """Historical installs may reuse downloads, never the runner's installed/user state."""
    case = tmp_path / "test_download_cache_paths.py"
    caches = {key: str(tmp_path / key) for key in
              ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "npm_config_cache")}
    forbidden = {key: str(tmp_path / key) for key in
                 ("UV_PROJECT_ENVIRONMENT", "HERMES_HOME", "HERMES_RUNTIME_DIR", "OPENAI_API_KEY")}
    case.write_text(
        "import os\n"
        "def test_download_caches():\n"
        f"    assert {{key: os.environ.get(key) for key in {tuple(caches)!r}}} == {caches!r}\n"
        f"    assert all(key not in os.environ for key in {tuple(forbidden)!r})\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, **caches, **forbidden, "HERMES_PYTHON": sys.executable,
           "HOME": str(tmp_path / "home"), "HERMES_TEST_FILE_RETRIES": "0"}
    Path(env["HOME"]).mkdir()
    env.pop("__HERMES_ACTIVATED", None)
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("location", [".hermes", ".cache/uv", ".npm", ".local/share/uv", "symlink", "relative"])
def test_shell_runner_refuses_real_account_download_caches(tmp_path, location):
    """A sandbox HOME must not disguise live caches, including symlinked descendants."""
    # Read in a fresh interpreter: pytest's machine-home fixtures may patch pwd in-process.
    real_home = Path(subprocess.check_output(
        [sys.executable, "-c", "import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)"],
        text=True,
    ).strip())
    if location == "symlink":
        alias = tmp_path / "cache-alias"
        alias.symlink_to(real_home / ".hermes", target_is_directory=True)
        unsafe = alias / "cache" / "uv"
    elif location == "relative":
        unsafe = Path("relative-cache")
    else:
        unsafe = real_home / location / "runner-refusal-canary"
    keys = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "npm_config_cache")
    case = tmp_path / "test_refused_cache_paths.py"
    case.write_text(
        "import os\n"
        "def test_caches_are_scrubbed():\n"
        f"    assert all(key not in os.environ for key in {keys!r})\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, **dict.fromkeys(keys, str(unsafe)), "HERMES_PYTHON": sys.executable,
           "HOME": str(tmp_path / "fake-home"), "HERMES_TEST_FILE_RETRIES": "0"}
    Path(env["HOME"]).mkdir()
    env.pop("__HERMES_ACTIVATED", None)
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
