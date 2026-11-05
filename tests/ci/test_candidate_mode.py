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


def overlay(git, commit, anchor, extra=None):
    git("reset", "--hard", anchor)
    workflows = git("ls-tree", "-r", "--name-only", anchor, ".github/workflows").splitlines()
    changes = dict.fromkeys(workflows)
    changes.update(dict.fromkeys(mode.OVERLAY_PATHS, "policy\n"))
    changes.update(extra or {})
    return commit("policy overlay", changes)


def test_upstream_candidate_uses_own_suite_path(graph):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    candidate = commit("candidate", {"change": "upstream fix\n"})
    staging = overlay(git, commit, upstream)
    git("reset", "--hard", candidate)
    assert mode.select(root, candidate, upstream, staging) == ("upstream", upstream)


def test_fork_candidate_keeps_full_fork_gate(graph):
    root, _, commit, upstream, _ = graph
    candidate = commit("fork candidate", {"change": "fork fix\n"})
    assert mode.select(root, candidate, upstream, upstream) == ("fork", "")


def test_candidate_selector_tampering_cannot_change_fork_classification(graph):
    root, _, commit, upstream, _ = graph
    candidate = commit("tampered selector", {
        "scripts/ci/candidate_mode.py": "raise RuntimeError('candidate policy executed')\n",
    })
    # Execute the loaded trusted policy, not the candidate's replacement file.
    assert mode.select(root, candidate, upstream, upstream) == ("fork", "")


def test_deleting_gate_cannot_downgrade_fork(graph):
    root, _, commit, upstream, _ = graph
    candidate = commit("delete gate", {"bin/ci": None})
    with pytest.raises(ValueError, match="refusing downgrade"):
        mode.select(root, candidate, upstream, upstream)


def test_wrong_head_is_rejected(graph):
    root, _, _, upstream, _ = graph
    with pytest.raises(ValueError, match="exact PR head"):
        mode.select(root, upstream, upstream, upstream)


def test_missing_anchor_history_fails_closed(graph, monkeypatch):
    root, _, _, upstream, fork = graph
    monkeypatch.setattr(mode, "FORK_GATE_ROOT", "f" * 40)
    with pytest.raises(subprocess.CalledProcessError):
        mode.select(root, fork, upstream, upstream)


def test_unknown_lineage_with_gate_is_rejected(graph):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    candidate = commit("unknown gate", {"bin/ci": "not the fork gate\n"})
    with pytest.raises(ValueError, match="Unknown gate lineage"):
        mode.select(root, candidate, upstream, upstream)


@pytest.mark.parametrize("merge_baseline", [False, True])
def test_baseline_tests_exact_head_with_first_parent_overlay(graph, merge_baseline):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    baseline = commit("baseline", {"baseline": "upstream change\n"})
    if merge_baseline:
        git("reset", "--hard", upstream)
        side = commit("side", {"side": "side change\n"})
        baseline = git("commit-tree", f"{baseline}^{{tree}}", "-p", upstream, "-p", side,
                       "-m", "merge baseline")
    policy = overlay(git, commit, upstream)
    git("reset", "--hard", baseline)
    staging = mode.prepare_staging(root, baseline, baseline, policy)
    assert git("status", "--porcelain") == ""
    assert git("merge-base", baseline, staging) == upstream
    assert mode.select(root, baseline, baseline, staging) == ("upstream", baseline)
    assert git("rev-parse", "HEAD") == baseline


@pytest.mark.parametrize("contamination", ["candidate", "off-path", "reverted", "merge",
                                           "workflow-modified", "workflow-added", "wrong-anchor"])
def test_staging_rejects_contaminated_ancestry_not_just_final_tree(graph, contamination):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    candidate = commit("candidate", {"implementation": "candidate change\n"})
    anchor = candidate if contamination == "candidate" else upstream
    extra = {"implementation": "sneaked in\n"} if contamination in {"off-path", "reverted"} else {}
    if contamination == "workflow-modified":
        extra[".github/workflows/lint.yml"] = "paid workflow edited\n"
    if contamination == "workflow-added":
        extra[".github/workflows/new.yml"] = "paid workflow added\n"
    staging = overlay(git, commit, anchor, extra)
    if contamination == "reverted":
        staging = commit("hide contamination", {"implementation": None})
    if contamination == "merge":
        staging = git("commit-tree", f"{staging}^{{tree}}", "-p", upstream, "-p", staging,
                      "-m", "merge overlay")
    if contamination == "wrong-anchor":
        # The candidate's expected base cannot be inferred from its staging base.
        upstream = git("commit-tree", f"{upstream}^{{tree}}", "-p", upstream,
                       "-m", "different expected base")
    git("reset", "--hard", candidate)
    with pytest.raises(ValueError, match="[Cc]ontaminated|merge-base"):
        mode.check_staging(root, candidate, upstream, staging)


def test_prepare_candidate_preserves_checkout_index_and_branch(graph):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    candidate = commit("candidate", {"implementation": "candidate change\n"})
    policy = overlay(git, commit, upstream)
    git("reset", "--hard", candidate)
    (root / "local").write_text("uncommitted\n", encoding="utf-8")
    git("add", "local")
    index_before = (root / ".git/index").read_bytes()
    staging = mode.prepare_staging(root, candidate, upstream, policy)
    assert (root / ".git/index").read_bytes() == index_before
    assert git("rev-parse", "HEAD") == candidate
    assert git("merge-base", candidate, staging) == upstream
    assert mode.select(root, candidate, upstream, staging) == ("upstream", upstream)


def test_staging_cannot_leave_upstream_workflows_active(graph):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    candidate = commit("candidate", {"implementation": "candidate change\n"})
    git("reset", "--hard", upstream)
    staging = commit("incomplete overlay", dict.fromkeys(mode.OVERLAY_PATHS, "policy\n"))
    git("reset", "--hard", candidate)
    # Correct merge-base and allowed changed paths alone are insufficient.
    with pytest.raises(ValueError, match="only the two free"):
        mode.check_staging(root, candidate, upstream, staging)


def test_prepare_refuses_candidate_workflow_activation_before_publication(graph):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    candidate = commit("candidate", {".github/workflows/new.yml": "paid workflow\n"})
    policy = overlay(git, commit, upstream)
    with pytest.raises(ValueError, match="do not publish"):
        mode.prepare_staging(root, candidate, upstream, policy)
