"""The candidate, not the frozen updater, owns dependency build policy."""
from pathlib import Path
import json
import shutil
import sys

import pytest

from hermes_cli import immutable_releases as releases


def _candidate(tmp_path):
    candidate = tmp_path / "candidate with spaces"
    package = candidate / "pm"
    package.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[2] / "pm" / "build_env.py", package / "build_env.py")
    (package / "__init__.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "class InstallError(Exception): pass\n"
        "def build_environment(**kwargs):\n"
        "    Path(kwargs['source'], 'build.json').write_text(json.dumps(kwargs, default=str))\n"
        "    return Path(kwargs['out'], 'bin/python')\n", encoding="utf-8")
    return candidate


def test_frozen_updater_uses_candidate_pm(tmp_path, monkeypatch):
    candidate = _candidate(tmp_path)
    # A real child must load the candidate even while the parent's PM is already imported.
    monkeypatch.setattr("pm.build_environment", lambda **kwargs: pytest.fail("stale updater PM used"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "foreign"))
    releases._build_venv(candidate, extras=["messaging", "tts"],
                         python=tmp_path / "frozen-source-python-3.11")
    built = json.loads((candidate / "build.json").read_text())
    assert built["source"] == str(candidate)
    assert built["out"] == str(candidate / ".venv")
    assert built["extras"] == ["messaging", "tts"]
    assert built["frozen"] and built["explicit"]
    assert built["python"] is None  # Candidate PM selects its pinned ABI.


def test_candidate_pm_failure_keeps_diagnostic(tmp_path, monkeypatch):
    candidate = _candidate(tmp_path)
    (candidate / "pm" / "__init__.py").write_text(
        "class InstallError(Exception): pass\n"
        "def build_environment(**kwargs):\n"
        "    raise InstallError('browser-harness requires pillow==12.3.0, but 13.0.0 is installed')\n",
        encoding="utf-8")
    monkeypatch.setattr("pm.build_environment", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="browser-harness requires pillow==12.3.0") as failed:
        releases._build_venv(candidate, python=Path(sys.executable))
    from gateway.update_notifications import _failure_cause
    notice = _failure_cause({"stop_reason": str(failed.value)}, 1)
    assert "browser-harness requires pillow==12.3.0" in notice
    assert "13.0.0" in notice
    assert not (candidate / ".release-ready").exists()


@pytest.mark.parametrize("exception", ["OSError", "RuntimeError"])
def test_unexpected_candidate_error_keeps_actionable_notice(tmp_path, monkeypatch, exception):
    candidate = _candidate(tmp_path)
    (candidate / "pm" / "__init__.py").write_text(
        "class InstallError(Exception): pass\n"
        "def build_environment(**kwargs):\n"
        f"    raise {exception}('pinned uv binary missing')\n", encoding="utf-8")
    monkeypatch.setattr("pm.build_environment", lambda **kwargs: None)
    with pytest.raises(RuntimeError) as failed:
        releases._build_venv(candidate, python=Path(sys.executable))
    from gateway.update_notifications import _failure_cause
    notice = _failure_cause({"stop_reason": str(failed.value)}, 1)
    assert "pinned uv binary missing" in notice
    assert "Traceback" not in notice


def test_dependency_failure_notice_keeps_actual_conflict():
    from gateway.update_notifications import _failure_cause

    cause = _failure_cause({"stop_reason": (
        "venv: dependency validation failed: Checked 144 packages in 39ms\n"
        "Found 1 incompatibility\n"
        "The package `browser-harness` requires `pillow==12.3.0`, but `13.0.0` is installed\n"
        " — retry, or run `hermes pm doctor`"
    )}, 1)
    assert "browser-harness" in cause
    assert "pillow==12.3.0" in cause
    assert "13.0.0" in cause
    assert "Checked 144" not in cause
