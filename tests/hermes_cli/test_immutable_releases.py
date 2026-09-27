"""S2 immutable-release invariants."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import immutable_releases as releases


def _fake_release(path: Path, marker: str, *, lock: str = "same") -> None:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(f"[project]\nname='hermes-{marker}'\n")
    (path / "uv.lock").write_text(lock)
    venv = path / ".venv" / "bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(python.stat().st_mode | stat.S_IEXEC)


def test_promote_is_atomic_and_rollback_round_trip(tmp_path):
    home = tmp_path / ".hermes"
    a, b = home / "releases" / "a", home / "releases" / "b"
    _fake_release(a, "a")
    _fake_release(b, "b")
    releases.promote(home, a)
    before = hashlib.sha256((a / "pyproject.toml").read_bytes()).hexdigest()
    result = releases.promote(home, b)
    assert result["current"] is not None
    assert Path(result["current"]).resolve() == b.resolve()
    assert hashlib.sha256((a / "pyproject.toml").read_bytes()).hexdigest() == before
    assert (home / "previous").resolve() == a.resolve()
    releases.rollback(home)
    assert (home / "current").resolve() == a.resolve()
    assert (home / "previous").resolve() == b.resolve()


def test_failed_plugin_smoke_does_not_flip_pointer(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    a, b = home / "releases" / "a", home / "releases" / "b"
    _fake_release(a, "a")
    _fake_release(b, "b")
    releases.promote(home, a)
    monkeypatch.setattr(releases, "smoke_plugins", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad plugin")))
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='source'\n")
    (source / "uv.lock").write_text("new")
    monkeypatch.setattr(releases, "_build_venv", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "release_sha", lambda _source: "c")
    with pytest.raises(RuntimeError, match="bad plugin"):
        releases.stage_release(source, home, uv="unused")
    assert (home / "current").resolve() == a.resolve()


def test_retention_keeps_live_and_rollback_pins(tmp_path):
    home = tmp_path / ".hermes"
    for i in range(7):
        p = home / "releases" / str(i)
        _fake_release(p, str(i))
        os.utime(p, (i, i))
    releases.promote(home, home / "releases" / "6")
    releases.promote(home, home / "releases" / "5")
    pinned = home / "releases" / "0"
    removed = releases.retain(home, extra_pins=[pinned])
    assert pinned not in removed
    assert (home / "releases" / "6").exists()
    assert (home / "releases" / "5").exists()
    assert len(list((home / "releases").iterdir())) >= 5


def test_worker_environment_is_resolved_release_not_current(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "a"
    _fake_release(release, "a")
    env = releases.detached_worker_env(home, release, {"PYTHONPATH": "old"})
    assert env["HERMES_RELEASE"] == str(release.resolve())
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(release.resolve()), "old"]


def test_prepare_venv_changed_lock_builds_fresh(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    _fake_release(old, "old", lock="old")
    _fake_release(new, "new", lock="new")
    called = []
    monkeypatch.setattr(releases, "_build_venv", lambda release, uv="uv": called.append(release))
    _target, mode = releases.prepare_venv(new, old)
    assert mode == "built"
    assert called == [new]


def test_prepare_venv_identical_lock_still_builds_in_place(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    _fake_release(old, "same", lock="same")
    _fake_release(new, "same", lock="same")
    called = []
    monkeypatch.setattr(releases, "_build_venv", lambda release, uv="uv": called.append(release))
    _target, mode = releases.prepare_venv(new, old)
    assert mode == "built"
    assert called == [new]


def test_retention_protects_real_process_cwd_and_receipt(tmp_path):
    home = tmp_path / "profile"
    release_paths = [home / "releases" / str(i) for i in range(9)]
    for i, path in enumerate(release_paths):
        _fake_release(path, str(i))
        os.utime(path, (i, i))
    releases.promote(home, release_paths[8])
    releases.promote(home, release_paths[7])
    receipts = home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "recovery.json").write_text(json.dumps({"release_path": str(release_paths[1])}))
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=release_paths[0])
    try:
        import psutil
        assert Path(psutil.Process(worker.pid).cwd()).resolve() == release_paths[0].resolve()
        assert release_paths[0].resolve() in releases._live_process_pins(home)
        removed = releases.retain(home)
        assert release_paths[0].exists() and release_paths[1].exists()
        assert release_paths[8].exists() and release_paths[7].exists()
        assert len([path for path in release_paths[2:7] if path.exists()]) >= 3
        assert release_paths[0] not in removed and release_paths[1] not in removed
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_real_staging_rejects_incompatible_plugin_and_keeps_pointer_and_receipt(tmp_path, monkeypatch):
    """Exercise checkout -> candidate venv -> plugin probe -> updater receipt, not a mocked smoke."""
    from hermes_cli import update_cmd, update_receipt

    remote = Path(__file__).resolve().parents[2]
    source = tmp_path / "hermes-agent"
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout",
                    str(remote), str(source)], check=True)
    sha = subprocess.check_output(["git", "-C", str(remote), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", sha], check=True)
    home = tmp_path / "profile"
    home.mkdir()
    plugin = home / "plugins" / "candidate-test"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [candidate-test]\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text("name: candidate-test\nversion: '1.0'\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "from hermes_cli.symbol_that_does_not_exist import broken\n", encoding="utf-8")
    a = home / "releases" / "A"
    _fake_release(a, "A")
    releases.promote(home, a)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
    update_receipt.begin_update_receipt()
    try:
        assert update_cmd._activate_immutable_release() is False
        update_receipt.finalize_update_receipt("partial")
        assert (home / "current").resolve() == a.resolve()
        assert (home / "previous").exists() is False
        receipt = json.loads((home / "logs" / "update_receipts" / "latest.json").read_text(encoding="utf-8"))
        assert receipt["outcome"] == "partial"
        assert any(s["name"] == "immutable_release" and not s["ok"] and
                   "candidate plugin smoke failed" in s["detail"] for s in receipt["steps"])
    finally:
        update_receipt.finalize_update_receipt("partial")


def test_candidate_import_smoke_blocks_bad_enabled_plugin_without_writing_profile(tmp_path):
    home = tmp_path / "profile"
    plugin = home / "plugins" / "candidate-test"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [candidate-test]\n")
    (plugin / "plugin.yaml").write_text("name: candidate-test\nversion: '1.0'\n")
    source = Path(__file__).resolve().parents[2]
    (plugin / "__init__.py").write_text(
        "from hermes_cli.symbol_that_does_not_exist import broken\n"
    )
    with pytest.raises(RuntimeError, match="candidate plugin smoke failed"):
        releases.smoke_plugins(source, home)
    assert not (home / "logs").exists()
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n")
    releases.smoke_plugins(source, home)
    assert not (home / "logs").exists()
