"""Freshness checker confinement and real, credential-free profile child spawns."""
from datetime import UTC, datetime
import json
import os
from pathlib import Path

import pytest

from cron import jobs, scheduler
from cron.scheduled_send import assess_freshness, run_freshness_script
from cron.scheduler_provider import _profile_cron_scope


def _spec(script):
    return {"channel": "imessage", "conversation": "42", "recipient": "fixture",
            "approved_at": datetime.now(UTC).isoformat(), "script": str(script)}


def _checker(path, body=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body or 'print(\'{"checked": true, "stale": false}\')\n', encoding="utf-8")
    return path


def test_checker_child_uses_owning_profile_and_restores_launch_scope(tmp_path, monkeypatch):
    from agent import secret_scope
    import hermes_constants
    from tui_gateway import launch_profile_policy

    a, b = tmp_path / "a", tmp_path / "b"
    names = ["HERMES_HOME", "OPENAI_API_KEY", "LAUNCH_ONLY", "CHECKER_ACCOUNT", "CHECKER_TOKEN"]
    body = ('import json, os, pathlib\n'
            f'values = {{k: os.environ.get(k) for k in {names!r}}}\n'
            'pathlib.Path(__file__).parent.parent.joinpath("seen.json").write_text(json.dumps(values))\n'
            'print(\'{"checked": true, "stale": false}\')\n')
    for home, label in [(a, "a"), (b, "b")]:
        _checker(home / "scripts/check.py", body)
        (home / ".env").write_text(
            f"CHECKER_ACCOUNT={label}-account\nCHECKER_TOKEN={label}-sentinel\n", encoding="utf-8")
        (home / "config.yaml").write_text(
            "terminal:\n  env_passthrough: [CHECKER_ACCOUNT, CHECKER_TOKEN]\n", encoding="utf-8")
    (a / ".env").write_text((a / ".env").read_text() + "LAUNCH_ONLY=a-only\n", encoding="utf-8")
    path_env = os.environ.get("PATH", "")
    for name in list(os.environ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("PATH", path_env)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-launch-provider-sentinel")
    monkeypatch.setenv("CHECKER_ACCOUNT", "a-account")
    monkeypatch.setenv("CHECKER_TOKEN", "a-sentinel")
    monkeypatch.setenv("LAUNCH_ONLY", "a-only")
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    monkeypatch.setattr(hermes_constants, "get_routing_process_hermes_home", lambda: a)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    ambient = dict(os.environ)
    seen = []
    for home in (a, b, a):
        with _profile_cron_scope(home):
            verdict = run_freshness_script(_spec(home / "scripts/check.py"), {})
        assert verdict["checked"] is True and verdict["stale"] is False
        seen.append(json.loads((home / "seen.json").read_text()))
        assert secret_scope.current_secret_scope() is None
        assert os.environ == ambient
    assert seen[0] == seen[2]
    assert seen[1] == {"HERMES_HOME": str(b), "OPENAI_API_KEY": None, "LAUNCH_ONLY": None,
                       "CHECKER_ACCOUNT": "b-account", "CHECKER_TOKEN": "b-sentinel"}
    assert seen[0]["OPENAI_API_KEY"] is None
    assert seen[0]["CHECKER_ACCOUNT"] == "a-account"


@pytest.fixture
def checker_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key, value in {"HERMES_DIR": home, "CRON_DIR": home / "cron",
                       "JOBS_FILE": home / "cron/jobs.json", "OUTPUT_DIR": home / "cron/output"}.items():
        monkeypatch.setattr(jobs, key, value)
    return home


@pytest.mark.parametrize("shape", ["relative", "profile-absolute", "installed", "deployed-link",
                                   "external", "traversal", "script-link", "checker-link",
                                   "other-skill", "other-resource", "project-shadow", "alias-link"])
def test_checker_declaration_trust_boundary(checker_home, tmp_path, monkeypatch, shape):
    home = checker_home
    installed = tmp_path / "installed/communications"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("---\nname: communications\n---\n# Communications\n", encoding="utf-8")
    shared = _checker(installed / "scripts/send_freshness.py")
    (home / "config.yaml").write_text(
        f"skills:\n  external_dirs: [{json.dumps(str(installed.parent))}]\n", encoding="utf-8")
    local = _checker(home / "scripts/check.py")
    external = _checker(tmp_path / "arbitrary.py")
    if shape == "relative":
        path = "check.py"
    elif shape == "profile-absolute":
        path = local
    elif shape == "installed":
        path = shared
    elif shape == "deployed-link":
        (home / "skills").mkdir()
        (home / "skills/communications").symlink_to(installed, target_is_directory=True)
        path = home / "skills/communications/scripts/send_freshness.py"
    elif shape == "external":
        path = external
    elif shape == "traversal":
        path = home / "scripts/../../arbitrary.py"
    elif shape == "script-link":
        path = home / "scripts/escape.py"
        path.symlink_to(external)
    elif shape == "checker-link":
        shared.unlink()
        shared.symlink_to(external)
        path = shared
    elif shape == "other-skill":
        path = _checker(installed.parent / "other/scripts/send_freshness.py")
    elif shape == "other-resource":
        path = _checker(installed / "scripts/other.py")
    elif shape == "alias-link":
        path = tmp_path / "unregistered-checker.py"
        path.symlink_to(shared)
    else:
        project = tmp_path / "project"
        (project / ".git").mkdir(parents=True)
        shadow = project / ".agents/skills/communications"
        _checker(shadow / "scripts/send_freshness.py")
        (shadow / "SKILL.md").write_text("---\nname: communications\n---\n# Project\n", encoding="utf-8")
        (home / "config.yaml").write_text(
            f"skills:\n  external_dirs: [{json.dumps(str(installed.parent))}]\n"
            f"  trusted_project_dirs: [{json.dumps(str(project))}]\n", encoding="utf-8")
        monkeypatch.chdir(project)
        job = jobs.create_job(prompt="fixture", schedule="in 1h", deliver="local",
                              send_freshness=_spec(shared))
        assert jobs.get_job(job["id"])["send_freshness"]["script"] == str(shared)
        with pytest.raises(ValueError, match="send_freshness.script"):
            jobs.update_job(job["id"], {"send_freshness": _spec(shadow / "scripts/send_freshness.py")})
        return
    kwargs = dict(prompt="fixture", schedule="in 1h", deliver="local", send_freshness=_spec(path))
    if shape in {"relative", "profile-absolute", "installed", "deployed-link"}:
        job = jobs.create_job(**kwargs)
        assert jobs.get_job(job["id"])["send_freshness"] == kwargs["send_freshness"]
        assert run_freshness_script(_spec(path), job)["stale"] is False
    else:
        with pytest.raises(ValueError, match="send_freshness.script"):
            jobs.create_job(**kwargs)
        assert jobs.load_jobs() == []


@pytest.mark.parametrize("mutation", ["lifecycle", "symlink", "missing"])
@pytest.mark.parametrize("location", ["scripts", "installed"])
def test_checker_is_rescanned_at_create_update_and_fire(checker_home, tmp_path, monkeypatch, mutation, location):
    home = checker_home
    if location == "scripts":
        script = _checker(home / "scripts/check.py")
    else:
        installed = tmp_path / "installed/communications"
        script = _checker(installed / "scripts/send_freshness.py")
        (installed / "SKILL.md").write_text("---\nname: communications\n---\n# Communications\n", encoding="utf-8")
        (home / "config.yaml").write_text(
            f"skills:\n  external_dirs: [{json.dumps(str(installed.parent))}]\n", encoding="utf-8")
    spec = _spec(script)
    job = jobs.create_job(prompt="fixture", schedule="in 1h", deliver="local", send_freshness=spec)
    if mutation == "lifecycle":
        script.write_text('import os\nos.system("hermes gateway restart")\n', encoding="utf-8")
    elif mutation == "symlink":
        script.unlink()
        script.symlink_to(_checker(tmp_path / "outside.py"))
    else:
        script.unlink()
    with pytest.raises(ValueError, match="send_freshness.script"):
        jobs.create_job(prompt="fixture", schedule="in 1h", deliver="local", send_freshness=spec)
    with pytest.raises(ValueError, match="send_freshness.script"):
        jobs.update_job(job["id"], {"send_freshness": spec})
    # Re-check the already-stored declaration: mutation must never make a fresh verdict.
    verdict = assess_freshness(job)
    assert verdict["stale"] is True and verdict["checked"] is False
    invoked = []
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda *a, **k: invoked.append("worker"))
    monkeypatch.setattr(scheduler, "run_job", lambda *a, **k: invoked.append("payload"))
    assert scheduler.run_one_job(job) is True
    assert invoked == []
    held = jobs.get_job(job["id"])
    assert held["state"] == "missed" and held["enabled"] is False
    assert held["send_freshness"] == spec
