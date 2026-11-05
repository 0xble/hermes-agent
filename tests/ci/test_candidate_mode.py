"""Exercise the real ancestry selector with independent Git commit graphs."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "candidate_mode", Path(__file__).resolve().parents[2] / "scripts/ci/candidate_mode.py",
)
assert _SPEC and _SPEC.loader
mode = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mode)


@pytest.fixture
def graph(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "test",
           "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "test",
           "GIT_COMMITTER_EMAIL": "test@example.invalid"}

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], env=env, text=True).strip()

    def commit(name, paths):
        for relative, contents in paths.items():
            path = root / relative
            if contents is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")
        git("add", "--all")
        tree = git("write-tree")
        parents = ["-p", git("rev-parse", "HEAD")] if (root / ".git/refs/heads/main").exists() else []
        sha = git("commit-tree", tree, *parents, "-m", name)
        git("update-ref", "refs/heads/main", sha)
        git("reset", "--hard", sha)
        return sha

    git("init", "--initial-branch=main")
    upstream = commit("upstream", {path: "present\n" for path in (
        "scripts/check", "scripts/run_tests.sh", ".github/workflows/lint.yml",
        ".github/workflows/tests.yml", ".github/workflows/js-tests.yml",
        ".github/actions/setup-pm/action.yml",
    )})
    fork = commit("fork gate", {"bin/ci": "gate\n"})
    monkeypatch.setattr(mode, "FORK_GATE_ROOT", fork)
    return root, git, commit, upstream, fork


def test_upstream_candidate_uses_own_suite_path(graph):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    candidate = commit("candidate", {"change": "upstream fix\n"})
    assert mode.select(root, candidate, upstream) == ("upstream", upstream)


def test_fork_candidate_keeps_full_fork_gate(graph):
    root, _, commit, upstream, _ = graph
    candidate = commit("fork candidate", {"change": "fork fix\n"})
    assert mode.select(root, candidate, upstream) == ("fork", "")


def test_deleting_gate_cannot_downgrade_fork(graph):
    root, _, commit, upstream, _ = graph
    candidate = commit("delete gate", {"bin/ci": None})
    with pytest.raises(ValueError, match="refusing downgrade"):
        mode.select(root, candidate, upstream)


def test_wrong_head_is_rejected(graph):
    root, _, _, upstream, _ = graph
    with pytest.raises(ValueError, match="exact PR head"):
        mode.select(root, upstream, upstream)


def test_missing_anchor_history_fails_closed(graph, monkeypatch):
    root, _, _, upstream, fork = graph
    monkeypatch.setattr(mode, "FORK_GATE_ROOT", "f" * 40)
    with pytest.raises(subprocess.CalledProcessError):
        mode.select(root, fork, upstream)


def test_unknown_lineage_with_gate_is_rejected(graph):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    candidate = commit("unknown gate", {"bin/ci": "not the fork gate\n"})
    with pytest.raises(ValueError, match="Unknown gate lineage"):
        mode.select(root, candidate, upstream)
