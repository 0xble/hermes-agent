"""A direct ``python -`` probe that imports a test helper must not open a production state.db.

Such a probe runs no conftest and has no pytest ancestor, so neither the ``HERMES_HOME`` sandbox nor
the live-DB guard covers it: a review probe driving ``tests.gateway`` fixtures from a gateway shell
(``HERMES_HOME`` = the live home) began a runtime epoch on the live store. Importing the ``tests``
package arms the guard's marker. The child below is that probe, launched with a rebuilt environment
whose ``HOME`` is a temporary root, so "production" is ``<tmp>/home/.hermes`` and nothing real is
touched; it pins pytest ancestry off, as a shell-launched probe has none.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = r"""
import json, sys
sys.path.insert(0, {repo!r})
import hermes_state_guard
hermes_state_guard._PYTEST_ANCESTOR = False  # launched from a shell, not from pytest
if {import_tests!r}:
    import tests.gateway  # what `from tests.gateway.<module> import <helper>` runs first
from hermes_state import SessionDB
try:
    SessionDB().close()
    print(json.dumps({{"opened": True}}))
except RuntimeError as exc:
    print(json.dumps({{"opened": False, "error": str(exc)[:200]}}))
"""


def _probe(tmp_path: Path, *, import_tests: bool) -> dict:
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LOCALAPPDATA") if os.environ.get(key)}
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"), HERMES_DISABLE_LAZY_INSTALLS="1")
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(repo=str(REPO_ROOT), import_tests=import_tests)],
        capture_output=True, text=True, env=env, timeout=120, check=False, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_probe_importing_tests_refuses_the_production_store(tmp_path):
    result = _probe(tmp_path, import_tests=True)
    assert result["opened"] is False, "a probe that imported tests opened the production state.db"
    assert "live-system guard" in result["error"]
    assert not (tmp_path / "home" / ".hermes" / "state.db").exists()


def test_probe_without_tests_import_still_opens_its_store(tmp_path):
    """Control: the refusal comes from importing ``tests``, not from the child's environment."""
    assert _probe(tmp_path, import_tests=False)["opened"] is True
