#!/usr/bin/env python3
"""Collect complete upstream slices and admit only verified baseline-equivalence.

This file is executed from the frozen policy, never imported from the candidate.
There is deliberately no baseline-run, baseline-failures or merge-base CLI input.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile

from candidate_mode import OVERLAY_PATHS, check_staging, git, select

SLICES = tuple(f"{suite}-{index}" for suite in ("python", "e2e") for index in range(1, 11))
ALL_SLICES = (*SLICES, "messaging-1", "js-1")
JS_STEP = "Full candidate-owned JS and TypeScript suites"
STATIC_JOB = "Upstream blocking lint, JS and Rust"
TEST_STEPS = {
    JS_STEP,
    "Full candidate-owned Python suite (all slices required)",
    "Full candidate-owned E2E suite (including upgrade)",
    "Messaging-gated gateway contracts (no skipped coverage)",
}
RUNNER_PATHS = ("scripts/run_tests.sh", "scripts/run_tests_parallel.py", "scripts/_activation.sh", "test_durations.json")
MESSAGING = (
    "tests/gateway/test_native_role_reauthorization.py",
    "tests/gateway/test_native_telegram_startup_recovery.py",
    "tests/gateway/test_native_pause_notice.py",
    "tests/gateway/test_native_reply_crash_recovery.py",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def collect(suite: str, index: int, output: Path) -> int:
    """Use the candidate's canonical planner/runner, with a frozen observer."""
    root = Path.cwd()
    output.mkdir(parents=True, exist_ok=False)
    python = os.environ["HERMES_PYTHON"]
    # The runner intentionally strips PYTHONPATH. A disposable test-venv .pth
    # makes the frozen observer importable without touching candidate sources.
    site = subprocess.check_output([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True, encoding="utf-8").strip()
    (Path(site) / "frozen_gate_evidence.pth").write_text(str(Path(__file__).parent.resolve()) + "\n", encoding="utf-8")
    env = {**os.environ, "HERMES_TEST_FILE_RETRIES": "0"}
    env.pop("HERMES_TEST_SLICE", None)
    roots = ["--include-integration", "tests/e2e"] if suite == "e2e" else []
    if suite == "messaging":
        files = list(MESSAGING)
    else:
        planned = subprocess.check_output(["scripts/run_tests.sh", *roots, "--generate-slices", "10"], env=env, text=True, encoding="utf-8")
        matrices = [json.loads(line) for line in planned.splitlines() if line.startswith('{"slice":')]
        require(len(matrices) == 1, "Missing or ambiguous canonical slice plan")
        files = matrices[0]["slice"][index - 1]["files"].split(":")
    require(files and all((root / path).is_file() for path in files), "Empty/invalid selected slice")
    selection = output / "selection.txt"
    selection.write_text("\n".join(files) + "\n", encoding="utf-8")
    command = ["scripts/run_tests.sh", *roots, f"--files-from={selection}", "--file-retries=0"]
    if suite == "messaging":
        command.append("--fail-on-skip")
    # A separate '-p module' looks like Hermes' profile flag to tests that
    # import hermes_cli.main. Pytest accepts the attached form; Hermes does not
    # mistake it for a CLI profile, so observation cannot break the suite.
    command += ["--", "-pgate_test_evidence", f"--gate-evidence-dir={output / 'nodes'}"]
    rc = subprocess.call(command, env=env)
    manifest = {"schema": 1, "head": git(root, "rev-parse", "HEAD"), "suite": suite,
                "index": index, "files": files, "exit": rc,
                "runner": {path: git(root, "ls-tree", "HEAD", "--", path) for path in RUNNER_PATHS}}
    (output / "slice.json").write_text(json.dumps(manifest), encoding="utf-8")
    return rc


def js_runner(root: Path, head: str) -> dict:
    paths = git(root, "ls-tree", "-r", "--name-only", head).splitlines()
    selected = [p for p in paths if Path(p).name in {"package.json", "package-lock.json"}
                or Path(p).name.startswith("vitest.config.")
                or p in {".github/scripts/run-workspace-checks.mjs", "scripts/build/electron-runtime.mjs"}]
    return {p: git(root, "ls-tree", head, "--", p) for p in selected}


def js_nodes(reports: list[dict], exit_code: int) -> tuple[dict, set]:
    require(reports and exit_code in (0, 1), "Missing JS evidence/harness failure")
    failures, called, seen = {}, set(), set()
    for report in reports:
        require(report.get("schema") == 1 and not report["infra"] and report["files"],
                "JS setup/collection/teardown/harness failure")
        for row in report["nodes"]:
            node = row["node"]
            require(node not in seen and row["state"] in {"pass", "fail", "skip", "todo"},
                    "Duplicate/incomplete JS node")
            seen.add(node)
            if row["state"] in {"pass", "fail"}:
                called.add(node)
            if row["state"] == "fail":
                require(row["category"], "Missing JS failure category")
                failures[node] = row["category"]
    require(called and ((exit_code == 1) == bool(failures)), "Unexplained JS check failure")
    return failures, called


def collect_js(output: Path) -> int:
    """Keep the canonical workspace checks, including checks after failing tests."""
    root = Path.cwd()
    output.mkdir(parents=True, exist_ok=False)
    observer = Path(__file__).with_name("gate_js_evidence.mjs").resolve()
    npm, node = shutil.which("npm"), shutil.which("node")
    require(npm and node, "Missing JS toolchain")
    with tempfile.TemporaryDirectory(prefix="frozen-npm-") as directory:
        proxy = Path(directory) / "npm"
        proxy.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(str(p)) for p in (node, observer))
                         + ' npm-proxy "$@"\n', encoding="utf-8")
        proxy.chmod(0o755)
        env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
               "FROZEN_REAL_NPM": str(npm), "FROZEN_JS_ROOT": str(root), "FROZEN_JS_OUTPUT": str(output),
               "FROZEN_JS_OBSERVE": "1",
               "NODE_OPTIONS": os.environ.get("NODE_OPTIONS", "") + " --import " + shlex.quote(observer.as_uri())}
        rc = subprocess.call([str(node), ".github/scripts/run-workspace-checks.mjs", "--concurrency", "2"], env=env)
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in output.glob("vitest-*.json")]
    failed = any(n["state"] == "fail" for r in reports for n in r["nodes"])
    # rc!=0 still means lint/build/setup failed: do not relabel it as a node failure.
    exit_code = 2 if rc else int(failed)
    manifest = {"schema": 1, "head": git(root, "rev-parse", "HEAD"), "suite": "js", "index": 1,
                "exit": exit_code, "runner": js_runner(root, "HEAD")}
    (output / "slice.json").write_text(json.dumps(manifest), encoding="utf-8")
    js_nodes(reports, exit_code)
    return exit_code


def session_nodes(session: dict) -> tuple[dict, set]:
    require(session["exit"] in (0, 1, 5) and not session["errors"], "Collection/harness failure")
    nodes = session["collected"]
    require(len(nodes) == len(set(nodes)), "Duplicate collected node")
    reports, failures, called = {}, {}, set()
    for report in session["reports"]:
        key = (report["node"], report["phase"])
        require(key not in reports and report["node"] in nodes, "Duplicate/uncollected report")
        reports[key] = report
    for node in nodes:
        setup, call, teardown = (reports.get((node, phase)) for phase in ("setup", "call", "teardown"))
        require(setup is not None and teardown is not None, "Missing phase")
        assert setup is not None and teardown is not None
        require(teardown["outcome"] == "passed", "Teardown failure")
        require(setup["outcome"] in ("passed", "skipped"), "Setup failure")
        if setup["outcome"] == "skipped":
            require(call is None, "Call after skipped setup")
            continue
        require(call is not None, "Missing call")
        assert call is not None
        require(call["outcome"] in ("passed", "failed", "skipped"), "Invalid call")
        if call["outcome"] != "skipped":
            called.add(node)
        if call["outcome"] == "failed":
            require(call["category"], "Missing failure category")
            failures[node] = call["category"]
    require((session["exit"] == 1) == bool(failures), "Unexplained pytest failure")
    return failures, called


def slice_nodes(manifest: dict, sessions: list[dict], head: str) -> tuple[dict, set]:
    require(manifest.get("schema") == 1 and manifest.get("head") == head, "Wrong slice head/schema")
    expected = manifest["files"]
    require(expected and len(expected) == len(set(expected)), "Invalid selected files")
    seen, failures, called = set(), {}, set()
    for session in sessions:
        require(len(session["files"]) == 1, "Unexpected pytest invocation")
        file = session["files"][0]
        matches = [path for path in expected if file == path or file.endswith("/" + path)]
        require(len(matches) == 1 and matches[0] not in seen, "Unplanned or repeated file")
        seen.add(matches[0])
        failed, executed = session_nodes(session)
        require(not (failures.keys() & failed.keys()), "Duplicate node across files")
        failures.update(failed)
        called.update(executed)
    require(seen == set(expected), "Missing selected file/slice")
    require(manifest["exit"] in (0, 1) and ((manifest["exit"] == 1) == bool(failures)), "Non-test runner failure")
    if manifest["suite"] == "messaging":
        require(all(len(s["collected"]) > 0 for s in sessions), "Empty messaging file")
        require(sum(len(s["collected"]) for s in sessions) == len(called), "Skipped messaging node")
    return failures, called


def compare(mode: str, candidate: dict, baseline: dict) -> list[str]:
    require(mode == "upstream", "Fork descendants require full green")
    require(set(candidate) == set(baseline) == set(ALL_SLICES), "Missing selected slice")
    inherited = {}
    for name, c in candidate.items():
        b = baseline[name]
        require(c["runner"] == b["runner"], "Planner/runner identity differs")
        # Adding tests may legitimately repartition files. Compare node sets
        # across the whole suite below, not just within an LPT bucket.
    for suite in ("python-", "e2e-", "messaging-", "js-"):
        current = {n: cat for key, value in candidate.items() if key.startswith(suite) for n, cat in value["failures"].items()}
        previous = {n: cat for key, value in baseline.items() if key.startswith(suite) for n, cat in value["failures"].items()}
        called_c = set().union(*(v["called"] for k, v in candidate.items() if k.startswith(suite)))
        called_b = set().union(*(v["called"] for k, v in baseline.items() if k.startswith(suite)))
        require(called_b <= called_c, "Baseline-executed nodes removed or newly skipped")
        def inherited_category(node, category):
            old = previous.get(node)
            return old == category or (suite == "js-" and isinstance(old, set) and category in old)
        require(all(inherited_category(node, category) for node, category in current.items()),
                f"Candidate-only failure/category: {sorted(n for n, cat in current.items() if not inherited_category(n, cat))}")
        inherited.update(current)
    return [f"{node} [{inherited[node]}]" for node in sorted(inherited)]


def api(repository: str, suffix: str, *, pages=False):
    command = ["gh", "api", f"repos/{repository}/{suffix}"]
    if pages:
        command += ["--paginate", "--slurp"]
    return json.loads(subprocess.check_output(command, text=True, encoding="utf-8"))


def verify_jobs(jobs: list[dict]):
    expected = {f"Upstream {suite} ({i}/10)" for suite in ("Python", "E2E") for i in range(1, 11)}
    required = expected | {"Freeze candidate gate mode", STATIC_JOB, "Prompt surface diff / Prompt surface summary"}
    test_jobs = expected | {STATIC_JOB}
    rows = {job["name"]: job for job in jobs}
    require(len(rows) == len(jobs) and required <= rows.keys(), "Missing/duplicate selected job")
    for name in required:
        job = rows[name]
        require(job["status"] == "completed", "Incomplete selected job")
        allowed = ("success", "failure") if name in test_jobs else ("success",)
        require(job["conclusion"] in allowed, f"Non-test or incomplete job: {name}")
        if name in test_jobs:
            test_step = (JS_STEP if name == STATIC_JOB else
                         "Full candidate-owned Python suite (all slices required)" if "Python" in name
                         else "Full candidate-owned E2E suite (including upgrade)")
            step_names = [step["name"] for step in job["steps"]]
            require(step_names.count(test_step) == 1 and step_names.count("Upload complete test-node evidence") == 1,
                    "Missing test/evidence step")
            if name == "Upstream E2E (1/10)":
                require(step_names.count("Messaging-gated gateway contracts (no skipped coverage)") == 1,
                        "Missing messaging step")
        for step in job["steps"]:
            if ((name in expected and step["name"] in TEST_STEPS - {JS_STEP})
                    or (name == STATIC_JOB and step["name"] == JS_STEP)):
                # Messaging is selected only on E2E slice 1.
                optional = step["name"].startswith("Messaging-") and name != "Upstream E2E (1/10)"
                require(step["conclusion"] in (("skipped",) if optional else ("success", "failure")), "Missing test step")
            else:
                require(step["conclusion"] == "success", f"Setup/harness/infra failure: {name}: {step['name']}")
    for name in ("Static and Node gate", "Python and e2e (${{ matrix.shard }}/10)"):
        require(rows.get(name, {}).get("conclusion") == "skipped", "Fork lane was not inactive")


def validate_run(run: dict, head: str, workflow_id: int, *, baseline=False):
    require(run["head_sha"] == head and run["workflow_id"] == workflow_id
            and run["path"] == ".github/workflows/gate.yml" and run["event"] == "pull_request",
            "Wrong baseline head/workflow identity")
    if baseline:
        require(run["status"] == "completed" and run["conclusion"] in ("success", "failure"),
                "Baseline not terminal/usable")
        # Do not mix attempts: a rerun's successful jobs may be copied from an
        # earlier attempt. Fresh proof is cheap on the free runners.
        require(run["run_attempt"] == 1, "Baseline rerun is not complete fresh evidence")


def verify_policy(root: Path, run: dict, policy: str, head: str):
    """Bind server-side run identity to its real merge-ref policy, not an artifact claim."""
    prs = run["pull_requests"]
    require(len(prs) == 1, "Baseline must retain its live PR association until admission")
    pr = prs[0]
    require(pr["head"]["sha"] == head, "Baseline PR head differs")
    references = run.get("referenced_workflows", [])
    path = f"{os.environ['GITHUB_REPOSITORY']}/.github/workflows/prompt-surface-diff.yml@"
    reference = [r for r in references if r["path"].startswith(path)]
    require(len(reference) == 1 and reference[0].get("ref") == f"refs/pull/{pr['number']}/merge", "Unbound workflow merge ref")
    merge = reference[0]["sha"]
    require(reference[0]["path"] == path + merge, "Ambiguous workflow revision")
    for sha in (merge, pr["base"]["sha"]):
        git(root, "fetch", "--no-tags", "origin", sha)
    parents = git(root, "rev-list", "--parents", "-n", "1", merge).split()
    require(parents == [merge, pr["base"]["sha"], head], "Run merge parents differ from server PR association")
    check_staging(root, head, head, pr["base"]["sha"])
    for file in OVERLAY_PATHS:
        expected = git(root, "rev-parse", f"{policy}:{file}")
        require(git(root, "rev-parse", f"{merge}:{file}") == expected
                and git(root, "rev-parse", f"{pr['base']['sha']}:{file}") == expected,
                f"Baseline policy identity differs: {file}")


def evidence(root: Path, repository: str, run: dict) -> dict:
    jobs = [j for page in api(repository, f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100", pages=True) for j in page["jobs"]]
    verify_jobs(jobs)
    artifacts = [a for p in api(repository, f"actions/runs/{run['id']}/artifacts?per_page=100", pages=True) for a in p["artifacts"] if a["name"].startswith("upstream-evidence-")]
    expected = {f"upstream-evidence-{name}" for name in (*SLICES, "js-1")}
    require({a["name"] for a in artifacts} == expected and len(artifacts) == len(expected), "Missing/duplicate evidence slice")
    result = {}
    for artifact in artifacts:
        require(not artifact["expired"] and artifact["workflow_run"]["head_sha"] == run["head_sha"], "Expired/wrong evidence")
        raw = subprocess.check_output(["gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for prefix in ("", "messaging/"):
                if prefix + "slice.json" not in archive.namelist():
                    continue
                manifest = json.loads(archive.read(prefix + "slice.json"))
                sessions = [json.loads(archive.read(n)) for n in archive.namelist() if n.startswith(prefix + "nodes/") and n.endswith(".json")]
                name = f"{manifest['suite']}-{manifest['index']}"
                require(name not in result, "Duplicate slice evidence")
                if prefix:
                    require(artifact["name"] == "upstream-evidence-e2e-1" and name == "messaging-1", "Misplaced messaging evidence")
                else:
                    require(artifact["name"] == "upstream-evidence-" + name, "Wrong artifact/slice identity")
                if name == "js-1":
                    require(manifest["head"] == run["head_sha"] and manifest["schema"] == 1,
                            "Wrong JS evidence head/schema")
                    expected_runner = js_runner(root, run["head_sha"])
                    reports = [json.loads(archive.read(n)) for n in archive.namelist()
                               if n.startswith("vitest-") and n.endswith(".json")]
                    failures, called = js_nodes(reports, manifest["exit"])
                    steps = [s for j in jobs if j["name"] == STATIC_JOB for s in j["steps"] if s["name"] == JS_STEP]
                    require(steps[0]["conclusion"] == ("failure" if failures else "success"), "JS step/evidence mismatch")
                else:
                    expected_runner = {p: git(root, "ls-tree", run['head_sha'], "--", p) for p in RUNNER_PATHS}
                    failures, called = slice_nodes(manifest, sessions, run["head_sha"])
                require(manifest["runner"] == expected_runner, "Forged planner/runner identity")
                result[name] = {"runner": manifest["runner"], "failures": failures, "called": called}
    require(set(result) == set(ALL_SLICES), "Missing selected slice evidence")
    return result


def qualify(root: Path, policy: str, upstream: str, head: str, staging: str):
    git(root, "fetch", "--no-tags", "https://github.com/NousResearch/hermes-agent.git", upstream)
    mode, base = select(root, head, upstream, staging)
    require(mode == "upstream", "Fork descendants require full green")
    repository = os.environ["GITHUB_REPOSITORY"]
    current = api(repository, f"actions/runs/{os.environ['GITHUB_RUN_ID']}")
    validate_run(current, head, current["workflow_id"])
    candidate = evidence(root, repository, current)
    # An exact baseline is proof, not an exception that needs another baseline.
    if not any(v["failures"] for v in candidate.values()):
        print(f"Fully green exact-head run {current['id']} ({head})")
        return
    require(head != base, "Baseline has test failures (evidence retained, not qualified)")
    runs = [r for p in api(repository, f"actions/workflows/{current['workflow_id']}/runs?head_sha={base}&event=pull_request&status=completed&per_page=100", pages=True) for r in p["workflow_runs"]]
    errors, verified = [], []
    for baseline in sorted(runs, key=lambda r: r["id"], reverse=True):
        try:
            validate_run(baseline, base, current["workflow_id"], baseline=True)
            require(baseline["id"] < current["id"], "Baseline must run first")
            verify_policy(root, baseline, policy, base)
            require(all(git(root, "ls-tree", head, "--", p) == git(root, "ls-tree", base, "--", p)
                        for p in RUNNER_PATHS), "Planner/runner changed: full green required")
            proof = evidence(root, repository, baseline)
            require(proof["js-1"]["runner"] == candidate["js-1"]["runner"],
                    "JS planner/configuration changed: full green required")
            verified.append((baseline["id"], proof))
        except (ValueError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
            errors.append(f"{baseline['id']}: {exc}")
    js_failures, js_called = {}, set()
    for _, proof in verified:
        js_called.update(proof["js-1"]["called"])
        for node, category in proof["js-1"]["failures"].items():
            js_failures.setdefault(node, set()).add(category)
    for run_id, proof in verified:
        try:
            combined = {**proof, "js-1": {**proof["js-1"], "failures": js_failures, "called": js_called}}
            inherited = compare(mode, candidate, combined)
        except ValueError as exc:
            errors.append(f"{run_id}: {exc}")
            continue
        message = (f"## Baseline-equivalent (NOT fully green)\n\nCandidate run: {current['id']} ({head})\n\n"
                   f"Python/E2E baseline run: {run_id} ({base})\n\n"
                   f"JS exact-B baseline runs (attempt 1): {', '.join(str(i) for i, _ in verified)} ({base})\n\n"
                   f"Frozen policy SHA: {policy}\n\nInherited failing nodes:\n" +
                   "\n".join(f"- `{node}`" for node in inherited) + "\n")
        print(message)
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(message)
        return
    raise ValueError("No verified exact baseline: " + "; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("suite", choices=("python", "e2e", "messaging"))
    collect_parser.add_argument("index", type=int, choices=range(1, 11))
    collect_parser.add_argument("output", type=Path)
    js_parser = sub.add_parser("collect-js")
    js_parser.add_argument("output", type=Path)
    qualification = sub.add_parser("qualify")
    for argument in ("root", "policy", "upstream", "head", "staging"):
        qualification.add_argument(argument)
    args = parser.parse_args()
    if args.action == "collect":
        sys.exit(collect(args.suite, args.index, args.output.resolve()))
    if args.action == "collect-js":
        sys.exit(collect_js(args.output.resolve()))
    qualify(Path(args.root), args.policy, args.upstream, args.head, args.staging)


if __name__ == "__main__":
    main()
