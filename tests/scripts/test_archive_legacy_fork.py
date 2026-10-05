"""A verified code archive must restore every stash, including reflog-only entries."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("damage_restore", [False, True])
def test_archive_restores_all_stashes_or_fails_closed(tmp_path, damage_restore):
    repo = tmp_path / "legacy"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Archive Test")
    git("config", "user.email", "archive@example.test")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", "/dev/null")
    (repo / "tracked").write_text("base")
    git("add", ".")
    git("commit", "-qm", "base")
    stashes = []
    for value in ("first", "second", "third"):
        (repo / "tracked").write_text(value)
        (repo / "untracked").write_text(value + " untracked")
        git("stash", "push", "-qu", "-m", value)
        stashes.append((git("rev-parse", "refs/stash"), value))
    before = git("stash", "list", "--format=%H")
    env = dict(os.environ)
    if damage_restore:
        import shutil
        real_git = shutil.which("git")
        tools = tmp_path / "tools"
        tools.mkdir()
        wrapper = tools / "git"
        wrapper.write_text(
            '#!/usr/bin/env bash\nset -e\n'
            '"$ARCHIVE_TEST_GIT" "$@"\n'
            'if [[ "$1" == clone ]]; then\n'
            '  restored="${@: -1}"\n'
            '  "$ARCHIVE_TEST_GIT" -C "$restored" gc --prune=now --quiet\n'
            'fi\n')
        wrapper.chmod(0o755)
        env.update(PATH=str(tools) + os.pathsep + env["PATH"], ARCHIVE_TEST_GIT=real_git)
    script = Path(__file__).resolve().parents[2] / "scripts/archive_legacy_fork.sh"
    result = subprocess.run(["bash", str(script), "--legacy", str(repo), "--out", str(tmp_path / "archives")],
                            text=True, capture_output=True, env=env)
    assert git("stash", "list", "--format=%H") == before
    if damage_restore:
        assert result.returncode != 0 and "archive: " not in result.stdout
        assert "stash" in result.stderr.lower()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        bundle = next((tmp_path / "archives").glob("*/legacy-fork.bundle"))
        restored = tmp_path / "restored"
        subprocess.run(["git", "clone", "-q", str(bundle), str(restored)], check=True)
        for sha, value in stashes:
            subprocess.run(["git", "-C", str(restored), "reset", "--hard", "HEAD"], check=True, capture_output=True)
            (restored / "untracked").unlink(missing_ok=True)
            subprocess.run(["git", "-C", str(restored), "stash", "apply", sha], check=True, capture_output=True)
            assert (restored / "tracked").read_text() == value
            assert (restored / "untracked").read_text() == value + " untracked"
