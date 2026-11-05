"""Exact-B JS evidence: node relief never excuses setup, lint, build or Rust."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ci"))
import baseline_equivalence as gate


def inventory():
    return {name: {"runner": {"runner": "same"}, "failures": {}, "called": {"existing"}}
            for name in gate.ALL_SLICES}


def run(number, head="B", **extra):
    return {"id": number, "head_sha": head, "workflow_id": 7, "path": ".github/workflows/gate.yml",
            "event": "pull_request", "status": "completed", "conclusion": "failure", "run_attempt": 1, **extra}


@pytest.mark.parametrize("invalid", [None, "head", "policy", "attempt", "candidate-only"])
def test_js_union_requires_verified_exact_b_runs_and_records_every_id(tmp_path, monkeypatch, invalid):
    current, earlier, later = run(30, "C"), run(10), run(20)
    proofs = {n: inventory() for n in (10, 20, 30)}
    proofs[10]["js-1"]["failures"] = {"existing": "AssertionError"}
    proofs[30]["js-1"]["failures"] = {"existing": "AssertionError"}
    # The newer exact-B pass must not erase the older exact-B failure.
    if invalid == "head":
        earlier["head_sha"] = "not-B"
    if invalid == "attempt":
        earlier["run_attempt"] = 2
    if invalid == "candidate-only":
        proofs[10]["js-1"]["failures"] = {}
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "30")
    receipt = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(receipt))
    monkeypatch.setattr(gate, "select", lambda *a: ("upstream", "B"))
    monkeypatch.setattr(gate, "git", lambda *a: "same")
    monkeypatch.setattr(gate, "api", lambda repo, endpoint, **kw: current if endpoint == "actions/runs/30"
                        else [{"workflow_runs": [later, earlier]}])
    verified = []
    def policy(root, baseline, policy, head):
        assert policy == "frozen-policy" and head == "B"
        if invalid == "policy" and baseline["id"] == 10:
            raise ValueError("Baseline policy identity differs")
        verified.append(baseline["id"])
    monkeypatch.setattr(gate, "verify_policy", policy)
    monkeypatch.setattr(gate, "evidence", lambda root, repo, row: copy.deepcopy(proofs[row["id"]]))
    if invalid:
        with pytest.raises(ValueError, match="No verified exact baseline"):
            gate.qualify(tmp_path, "frozen-policy", "U", "C", "staging")
        assert not receipt.exists()
    else:
        gate.qualify(tmp_path, "frozen-policy", "U", "C", "staging")
        text = receipt.read_text(encoding="utf-8")
        assert "JS exact-B baseline runs (attempt 1): 20, 10 (B)" in text
        assert "Frozen policy SHA: frozen-policy" in text
        assert "existing [AssertionError]" in text
        assert verified == [20, 10]


def test_wrong_policy_blob_rejected_before_contribution(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    baseline = run(10, pull_requests=[{"number": 1, "head": {"sha": "B"}, "base": {"sha": "overlay"}}],
                   referenced_workflows=[{"path": "owner/repo/.github/workflows/prompt-surface-diff.yml@merge",
                                          "ref": "refs/pull/1/merge", "sha": "merge"}])
    monkeypatch.setattr(gate, "check_staging", lambda *a: None)
    def git(root, *args):
        if args[0] == "rev-list":
            return "merge overlay B"
        if args[0] == "rev-parse":
            return "correct" if args[1].startswith("policy:") else "wrong"
        return ""
    monkeypatch.setattr(gate, "git", git)
    with pytest.raises(ValueError, match="policy identity differs"):
        gate.verify_policy(tmp_path, baseline, "policy", "B")


def jobs():
    result = []
    for suite in ("Python", "E2E"):
        for index in range(1, 11):
            step = ("Full candidate-owned Python suite (all slices required)" if suite == "Python" else
                    "Full candidate-owned E2E suite (including upgrade)")
            steps = [{"name": step, "conclusion": "success"},
                     {"name": "Upload complete test-node evidence", "conclusion": "success"}]
            if suite == "E2E" and index == 1:
                steps.append({"name": "Messaging-gated gateway contracts (no skipped coverage)", "conclusion": "success"})
            result.append({"name": f"Upstream {suite} ({index}/10)", "status": "completed", "conclusion": "success", "steps": steps})
    for name in ("Freeze candidate gate mode", gate.STATIC_JOB, "Prompt surface diff / Prompt surface summary"):
        steps = [{"name": gate.JS_STEP, "conclusion": "failure"},
                 {"name": "Upload complete test-node evidence", "conclusion": "success"}] if name == gate.STATIC_JOB else []
        result.append({"name": name, "status": "completed", "conclusion": "failure" if steps else "success", "steps": steps})
    result.extend({"name": name, "conclusion": "skipped"} for name in
                  ("Static and Node gate", "Python and e2e (${{ matrix.shard }}/10)"))
    return result


@pytest.mark.parametrize("step", ["Set up PM", "Harness", "Full candidate-owned blocking lint",
                                  "Install and build candidate-owned JS prerequisites", "Candidate-owned Rust bootstrap suite",
                                  "Full candidate-owned WhatsApp Node suite", "Upload complete test-node evidence"])
def test_non_node_failures_never_get_differential_relief(step):
    rows = jobs()
    gate.verify_jobs(rows)
    static = next(row for row in rows if row["name"] == gate.STATIC_JOB)
    static["steps"].append({"name": step, "conclusion": "failure"})
    with pytest.raises(ValueError, match="Setup/harness/infra|Missing"):
        gate.verify_jobs(rows)


@pytest.mark.parametrize("infra", ["Setup failure", "Teardown failure", "Unhandled error", "Incomplete task"])
def test_js_machine_evidence_rejects_infrastructure(infra):
    report = {"schema": 1, "files": ["a.test.ts"], "infra": [],
              "nodes": [{"node": "existing", "state": "fail", "category": "AssertionError"}]}
    assert gate.js_nodes([report], 1) == ({"existing": "AssertionError"}, {"existing"})
    report["infra"] = [infra]
    with pytest.raises(ValueError, match="setup/collection/teardown/harness"):
        gate.js_nodes([report], 1)
    report["infra"] = []
    with pytest.raises(ValueError, match="harness"):
        gate.js_nodes([report], 2)


@pytest.mark.parametrize("hook", ["call", "beforeEach", "fixture", "call+cleanup"])
def test_frozen_js_reporter_observes_nodes_but_rejects_hook_errors(tmp_path, hook):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the JS observer contract")
    reporter = Path(gate.__file__).with_name("gate_js_evidence.mjs")
    output = tmp_path / "report.json"
    script = """
      const {default: Reporter} = await import(process.argv[1]);
      const r = new Reporter();
      r.onInit({projects: [{config: {retry: 0}}]});
      r.onTestRunStart([{moduleId: '/fixture/a.test.ts'}]);
      const result = {state:'fail', errors:[{name:'AssertionError'}], ...JSON.parse(process.argv[2])};
      r.onTestRunEnd([{moduleId:'/fixture/a.test.ts', task:{type:'suite', filepath:'/fixture/a.test.ts',
        name:'a.test.ts',result:{state:'fail'},tasks:[{type:'test', name:'node [exact]',result}]}}], [], 'failed');
    """
    phase = {"call": {"frozenCallError": True}, "beforeEach": {"hooks": {"beforeEach": "fail"}},
             "fixture": {"frozenNonCallError": True},
             "call+cleanup": {"frozenCallError": True, "frozenNonCallError": True}}[hook]
    subprocess.run([str(node), "--input-type=module", "-e", script, reporter.as_uri(), json.dumps(phase)],
                   check=True, timeout=30,
                   env={**os.environ, "FROZEN_JS_ROOT": "/fixture", "FROZEN_JS_REPORT": str(output)})
    report = json.loads(output.read_text(encoding="utf-8"))
    if hook != "call":
        with pytest.raises(ValueError, match="setup/collection/teardown/harness"):
            gate.js_nodes([report], 1)
    else:
        assert gate.js_nodes([report], 1) == ({'["","a.test.ts","node [exact]"]': "AssertionError"},
                                             {'["","a.test.ts","node [exact]"]'})
