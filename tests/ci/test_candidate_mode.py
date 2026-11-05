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


def test_prepare_candidate_preserves_checkout_index_and_branch(graph, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
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


@pytest.mark.parametrize("workflow_support", ["README.md", "scripts/helper.py"])
def test_prepare_strips_upstream_workflow_support_files(graph, workflow_support):
    root, git, commit, upstream, _ = graph
    git("reset", "--hard", upstream)
    path = f".github/workflows/{workflow_support}"
    upstream = commit("workflow support", {path: "upstream workflow support\n"})
    candidate = commit("candidate", {"implementation": "candidate change\n"})
    policy = overlay(git, commit, upstream)
    git("reset", "--hard", candidate)
    staging = mode.prepare_staging(root, candidate, upstream, policy)
    assert git("ls-tree", staging, path) == ""
    assert git("show", f"{candidate}:{path}") == "upstream workflow support"
    assert mode.select(root, candidate, upstream, staging) == ("upstream", upstream)


# Exercise the admission boundary, not the workflow's YAML spelling.
import copy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ci"))
import baseline_equivalence as equivalence


def paired_evidence():
    return {name: {"runner": {"scripts/run_tests.sh": "same"},
                   "failures": {}, "called": {"tests/a.py::test_existing"}}
            for name in (*equivalence.SLICES, "messaging-1")}


def test_baseline_equivalence_admits_subset_and_rejects_new_failure():
    baseline = paired_evidence()
    baseline["python-1"]["failures"] = {"tests/a.py::test_existing": "AssertionError",
                                          "tests/a.py::test_fixed": "ValueError"}
    candidate = copy.deepcopy(baseline)
    del candidate["python-1"]["failures"]["tests/a.py::test_fixed"]
    assert equivalence.compare("upstream", candidate, baseline) == [
        "tests/a.py::test_existing [AssertionError]",
    ]
    for node, category in (("tests/a.py::test_new", "AssertionError"),
                           ("tests/a.py::test_existing", "RuntimeError")):
        changed = copy.deepcopy(candidate)
        changed["python-1"]["failures"][node] = category
        with pytest.raises(ValueError, match="Candidate-only"):
            equivalence.compare("upstream", changed, baseline)
    with pytest.raises(ValueError, match="full green"):
        equivalence.compare("fork", candidate, baseline)


@pytest.mark.parametrize("side", ["candidate", "baseline"])
def test_equivalence_requires_every_slice_and_baseline_executed_node(side):
    candidate, baseline = paired_evidence(), paired_evidence()
    del (candidate if side == "candidate" else baseline)["e2e-10"]
    with pytest.raises(ValueError, match="Missing selected slice"):
        equivalence.compare("upstream", candidate, baseline)
    candidate, baseline = paired_evidence(), paired_evidence()
    baseline["e2e-1"]["called"].add("tests/e2e/a.py::test_removed")
    with pytest.raises(ValueError, match="removed or newly skipped"):
        equivalence.compare("upstream", candidate, baseline)


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("workflow_id", 8),
                                         ("path", ".github/workflows/other.yml"),
                                         ("status", "in_progress"), ("conclusion", "cancelled"),
                                         ("run_attempt", 2)])
def test_baseline_wrong_identity_or_incomplete_fails(field, value):
    run = {"head_sha": "a" * 40, "workflow_id": 7, "path": ".github/workflows/gate.yml",
           "event": "pull_request", "status": "completed", "conclusion": "failure", "run_attempt": 1}
    equivalence.validate_run(run, "a" * 40, 7, baseline=True)
    run[field] = value
    with pytest.raises(ValueError):
        equivalence.validate_run(run, "a" * 40, 7, baseline=True)


def test_candidate_cannot_supply_baseline_run_id_or_failures(monkeypatch):
    # No input surface exists; these candidate-controlled environment values
    # must never affect the frozen selector or baseline discovery.
    monkeypatch.setenv("BASELINE_RUN_ID", "1")
    monkeypatch.setenv("BASELINE_FAILURES", '{"tests/a.py::test_new":"AssertionError"}')
    candidate, baseline = paired_evidence(), paired_evidence()
    candidate["python-1"]["failures"] = {"tests/a.py::test_new": "AssertionError"}
    with pytest.raises(ValueError, match="Candidate-only"):
        equivalence.compare("upstream", candidate, baseline)


def test_machine_node_evidence_rejects_infra_and_missing_file():
    manifest = {"schema": 1, "head": "a" * 40, "files": ["tests/a.py"], "exit": 1, "suite": "python"}
    session = {"files": ["/checkout/tests/a.py"], "collected": ["tests/a.py::test_x[exact]"],
               "errors": [], "exit": 1, "reports": [
                   {"node": "tests/a.py::test_x[exact]", "phase": phase, "outcome": outcome,
                    "category": "AssertionError" if phase == "call" else ""}
                   for phase, outcome in (("setup", "passed"), ("call", "failed"), ("teardown", "passed"))]}
    assert equivalence.slice_nodes(manifest, [session], "a" * 40)[0] == {
        "tests/a.py::test_x[exact]": "AssertionError",
    }
    with pytest.raises(ValueError, match="Missing selected"):
        equivalence.slice_nodes(manifest, [], "a" * 40)
    for phase in ("setup", "teardown"):
        broken = copy.deepcopy(session)
        next(r for r in broken["reports"] if r["phase"] == phase)["outcome"] = "failed"
        with pytest.raises(ValueError, match="[Ss]etup|[Tt]eardown"):
            equivalence.slice_nodes(manifest, [broken], "a" * 40)
    session["exit"] = 124
    with pytest.raises(ValueError, match="harness"):
        equivalence.slice_nodes(manifest, [session], "a" * 40)


@pytest.mark.parametrize("incomplete", ["cancelled", "skipped", "timed_out"])
def test_missing_or_incomplete_hosted_slice_fails(incomplete):
    names = [f"Upstream {suite} ({i}/10)" for suite in ("Python", "E2E") for i in range(1, 11)]
    names += ["Freeze candidate gate mode", "Upstream blocking lint, JS and Rust",
              "Prompt surface diff / Prompt surface summary"]
    jobs = [{"name": name, "status": "completed", "conclusion": "success", "steps": []} for name in names]
    for job in jobs[:20]:
        test_step = ("Full candidate-owned Python suite (all slices required)" if "Python" in job["name"]
                     else "Full candidate-owned E2E suite (including upgrade)")
        job["steps"] = [{"name": test_step, "conclusion": "success"},
                        {"name": "Upload complete test-node evidence", "conclusion": "success"}]
        if job["name"] == "Upstream E2E (1/10)":
            job["steps"].append({"name": "Messaging-gated gateway contracts (no skipped coverage)",
                                 "conclusion": "success"})
    jobs += [{"name": name, "conclusion": "skipped"} for name in (
        "Static and Node gate", "Python and e2e (${{ matrix.shard }}/10)",
    )]
    equivalence.verify_jobs(jobs)
    with pytest.raises(ValueError, match="Missing"):
        equivalence.verify_jobs(jobs[1:])
    jobs[0]["conclusion"] = incomplete
    with pytest.raises(ValueError, match="incomplete"):
        equivalence.verify_jobs(jobs)
    jobs[0]["conclusion"] = "failure"
    jobs[0]["steps"].append({"name": "Set up Python", "conclusion": "failure"})
    with pytest.raises(ValueError, match="Setup/harness/infra"):
        equivalence.verify_jobs(jobs)



def test_cli_rejects_candidate_baseline_override(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["baseline_equivalence.py", "qualify", ".", "policy", "upstream",
                                    "head", "staging", "--baseline-run-id", "123"])
    with pytest.raises(SystemExit) as error:
        equivalence.main()
    assert error.value.code == 2
