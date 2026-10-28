"""Active extras must survive the payload-to-committed-environment transition."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from hermes_cli import immutable_releases as releases
from pm import paths
from pm.environments import committed_venv, project_python, runtime_facts_path, selected_venv
from pm.lock import Facts
from tests.pm._fixtures import _wheel


@pytest.mark.parametrize("extra,distribution", [("hindsight", "hindsight_client"), ("widget", "widget_client")])
def test_carry_keeps_payload_only_extra_in_first_committed_generation(tmp_path, monkeypatch, extra, distribution):
    """A missing Slack triggers a rebuild that must also keep the payload's other extra.

    PM's anchor inference does not cover every declared extra. Carry must pass
    the whole applicable active selection, not just packages absent from .venv.
    """
    import pm

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_OFFLINE", "1")
    (home / "config.yaml").write_text("memory:\n  provider: none\nplugins:\n  enabled: []\n", encoding="utf-8")
    uv = shutil.which("uv")
    assert uv, "the regression requires the real offline resolver"
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), Path(sys.executable)))
    monkeypatch.setattr("pm.client.is_runtime", lambda: True)
    monkeypatch.setattr(paths, "lockfile_path", lambda: tmp_path / "tool-lock.json")

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    client_wheel = _wheel(wheels, distribution)
    _wheel(wheels, "slack_bolt")
    previous, candidate = (home / "releases" / name for name in ("previous", "candidate"))
    for release in (previous, candidate):
        release.mkdir(parents=True)
        (release / "pyproject.toml").write_text(
            '[project]\nname="hermes-agent"\nversion="1"\nrequires-python=">=3.11"\n'
            f'[project.optional-dependencies]\n{extra}=["{distribution}==1.0"]\nslack=["slack-bolt==1.0"]\n'
            '[tool.uv]\npackage=false\nno-index=true\noffline=true\n'
            f'find-links=[{json.dumps(wheels.as_posix())}]\n', encoding="utf-8",
        )
        locked = subprocess.run([uv, "lock", "--python", sys.executable], cwd=release,
                                capture_output=True, text=True, timeout=60)
        assert locked.returncode == 0, locked.stderr

    monkeypatch.setattr(paths, "repo_root", lambda: previous)
    pm.sync_venv([extra, "slack"], explicit=True, project_root=previous)
    assert Facts(runtime_facts_path(previous)).get("venv")["extras"] == sorted([extra, "slack"])

    subprocess.run([uv, "venv", "--python", sys.executable, str(candidate / ".venv")],
                   check=True, capture_output=True, timeout=60)
    payload_python = project_python(candidate)
    subprocess.run([uv, "pip", "install", "--no-deps", "--python", str(payload_python), str(client_wheel)],
                   check=True, capture_output=True, timeout=60)
    monkeypatch.setattr(paths, "repo_root", lambda: candidate)
    assert committed_venv(candidate) is None
    assert selected_venv(candidate) == candidate / ".venv"
    assert releases._committed_extras(previous, candidate) == {extra, "slack"}
    payload = releases._active_distributions(payload_python)
    assert payload[distribution.replace("_", "-")] == "1.0"
    assert "slack-bolt" not in payload

    releases.carry_active_extras(candidate, previous)

    selected = committed_venv(candidate)
    assert selected is not None and selected != candidate / ".venv"
    assert Facts(runtime_facts_path(candidate)).get("venv")["extras"] == sorted([extra, "slack"])
    probe = subprocess.run(
        [str(project_python(candidate)), "-I", "-c", f"import {distribution}, slack_bolt"],
        capture_output=True, text=True, timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    # A satisfied candidate must not rebuild every time the preflight re-enters.
    facts_before = runtime_facts_path(candidate).read_bytes()
    assert releases.carry_active_extras(candidate, previous) == []
    assert runtime_facts_path(candidate).read_bytes() == facts_before
