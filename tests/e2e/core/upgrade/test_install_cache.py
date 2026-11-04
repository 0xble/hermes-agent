"""Keep repeated real installs cheap without reusing installed or user state."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess

import pytest

from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade._install_cache import InstallCache

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX historical-install cache uses directory symlinks")


def test_repeated_installs_share_only_downloads(tmp_path):
    cache = InstallCache(tmp_path / "downloads")
    sandboxes = [I.new_sandbox(tmp_path / name) for name in ("one", "two")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        envs = list(pool.map(lambda sb: cache.environment(sb.env), sandboxes))
    for key in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "HERMES_RUNTIME_DIR", "npm_config_cache"):
        assert envs[0][key] == envs[1][key]
        assert Path(envs[0][key]).is_relative_to(tmp_path / "downloads")
    for sb, env in zip(sandboxes, envs):
        pm_cache = Path(env["HOME"]) / ".hermes" / "cache" / "uv"
        assert pm_cache.resolve() == Path(env["UV_CACHE_DIR"]).resolve()
        assert cache.environment(env) == env  # retry keeps the same cache
        assert not sb.checkout.exists()
        assert not (sb.hermes_home / "installs").exists()
    (sandboxes[0].hermes_home / "config.yaml").write_text("one\n")
    assert not (sandboxes[1].hermes_home / "config.yaml").exists()
    assert envs[0]["HERMES_HOME"] != envs[1]["HERMES_HOME"]
    assert envs[0]["XDG_RUNTIME_DIR"] != envs[1]["XDG_RUNTIME_DIR"]


def test_managed_uv_is_a_private_copy(tmp_path):
    uv = tmp_path / "fixture-uv"
    uv.write_text("#!/bin/sh\nprintf 'fixture uv\\n'\n")
    uv.chmod(0o755)
    cache = InstallCache(tmp_path / "downloads")
    sb = I.new_sandbox(tmp_path / "install")
    cache.environment(sb.env, managed_uv=str(uv))
    binary = sb.hermes_home / "bin" / "uv"
    assert binary.read_bytes() == uv.read_bytes()
    assert not binary.is_symlink()
    binary.write_text("changed\n")
    assert "fixture uv" in uv.read_text()


def test_cache_refuses_an_unrelated_existing_directory(tmp_path):
    env = H.isolated_env(tmp_path / "install")
    unrelated = Path(env["HOME"]) / ".hermes" / "cache" / "uv"
    unrelated.mkdir(parents=True)
    (unrelated / "preserve").write_text("user cache\n")
    with pytest.raises(AssertionError, match="unexpected pre-existing cache"):
        InstallCache(tmp_path / "downloads").environment(env)
    assert (unrelated / "preserve").read_text() == "user cache\n"


def test_upgrade_origin_advertises_only_the_local_subject_branch(tmp_path, monkeypatch):
    from tests.e2e.core.upgrade import test_upgrade_path as upgrade

    source = tmp_path / "source"
    source.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": "0"}

    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    (source / "subject").write_text("local fixture\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    base = git("rev-parse", "HEAD")
    git("branch", "unrelated-partial-branch")
    monkeypatch.setattr(H, "WORKTREE", source)
    monkeypatch.setattr(upgrade, "_refs", lambda: upgrade._Refs(base, "fixture", base))
    root = tmp_path / "leg"
    root.mkdir()
    origin = upgrade._make_origin(root)
    refs = subprocess.run(["git", "-C", str(origin), "for-each-ref", "--format=%(refname)", "refs/heads"],
                          check=True, capture_output=True, text=True).stdout.splitlines()
    assert refs == ["refs/heads/main"], "unrelated partial-clone refs leaked into the served origin"
    assert upgrade._git("rev-parse", "main", cwd=origin) == base
