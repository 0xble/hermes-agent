"""Live pinned-Vitest observer probes (run after checkout-owned npm setup)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ci"))
import baseline_equivalence as gate


@pytest.mark.parametrize("phase", ["call", "timeout", "suite-hooks-pass", "beforeAll", "afterAll", "beforeEach",
                                  "fixture-setup", "fixture-cleanup", "completion", "same-error-cleanup"])
def test_real_vitest_phases(tmp_path, phase):
    root = Path(__file__).resolve().parents[2]
    cli = root / "node_modules/vitest/vitest.mjs"
    if not cli.is_file():
        pytest.skip("Run after checkout-owned npm setup for live Vitest proof")
    reporter = root / "scripts/ci/gate_js_evidence.mjs"
    module = (root / "node_modules/vitest/dist/index.js").as_uri()
    prefix = f"import {{ test, expect, beforeAll, afterAll, beforeEach, onTestFinished }} from {json.dumps(module)};\n"
    bodies = {
        "call": "test('exact node', () => expect(1).toBe(2));",
        "timeout": "test('exact node', async () => { await new Promise(() => {}); }, 30);",
        "suite-hooks-pass": "let ready=false; beforeAll(()=>{ready=true;}); afterAll(()=>expect(ready).toBe(true)); test('exact node',()=>expect(ready).toBe(true));",
        "beforeAll": "beforeAll(()=>{throw new Error('suite setup');}); test('exact node',()=>{});",
        "afterAll": "afterAll(()=>{throw new Error('suite cleanup');}); test('exact node',()=>{});",
        "beforeEach": "beforeEach(() => { throw new Error('setup'); }); test('exact node', () => {});",
        "fixture-setup": "const t=test.extend({a:async ({},use)=>{throw new Error('setup');}}); t('exact node',({a})=>{});",
        "fixture-cleanup": "const t=test.extend({a:async ({},use)=>{await use(1);throw new Error('cleanup');}}); t('exact node',({a})=>{});",
        "completion": "test('exact node',()=>{onTestFinished(()=>{throw new Error('finished');});});",
        "same-error-cleanup": "const e=new Error('same'); const t=test.extend({a:async ({},use)=>{await use(1);throw e;}});t('exact node',({a})=>{throw e;});",
    }
    (tmp_path / "case.test.mjs").write_text(prefix + bodies[phase], encoding="utf-8")
    config = tmp_path / "vitest.config.mjs"
    config.write_text("export default { test: { root: " + json.dumps(str(tmp_path)) +
                      ", include: ['case.test.mjs'], maxWorkers: 1, retry: 0 } };", encoding="utf-8")
    report = tmp_path / "report.json"
    command = [str(shutil.which("node")), str(cli), "run", "--config", str(config), "--reporter=default"]
    expected_exit = 0 if phase == "suite-hooks-pass" else 1
    if phase in {"suite-hooks-pass", "beforeAll", "afterAll"}:
        control = subprocess.run(command, cwd=tmp_path,
                                 env={k: v for k, v in os.environ.items() if k not in {"FROZEN_JS_OBSERVE", "NODE_OPTIONS"}},
                                 text=True, capture_output=True, encoding="utf-8", timeout=90, check=False)
        assert control.returncode == expected_exit, control.stdout + control.stderr
    result = subprocess.run([*command, f"--reporter={reporter}"], cwd=tmp_path,
                            env={**os.environ, "FROZEN_JS_OBSERVE": "1", "FROZEN_JS_ROOT": str(tmp_path),
                                 "FROZEN_JS_REPORT": str(report), "NODE_OPTIONS": "--import " + reporter.as_uri()},
                            text=True, capture_output=True, encoding="utf-8", timeout=90, check=False)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert report.is_file(), result.stdout + result.stderr
    evidence = json.loads(report.read_text(encoding="utf-8"))
    if phase == "suite-hooks-pass":
        failures, called = gate.js_nodes([evidence], 0)
        assert not failures and len(called) == 1
    elif phase in {"call", "timeout"}:
        failures, called = gate.js_nodes([evidence], 1)
        assert len(failures) == len(called) == 1
    else:
        with pytest.raises(ValueError, match="setup/collection/teardown/harness"):
            gate.js_nodes([evidence], 1)


@pytest.mark.parametrize("lint_failure", [False, True])
def test_real_npm_proxy_finishes_remaining_checks_then_restores_failure(tmp_path, monkeypatch, lint_failure):
    root = Path(__file__).resolve().parents[2]
    cli = root / "node_modules/vitest/vitest.mjs"
    if not cli.is_file():
        pytest.skip("Run after checkout-owned npm setup for live npm proof")
    # Use the checkout-owned dependencies without changing any source or config.
    module = (root / "node_modules/vitest/dist/index.js").as_uri()
    (tmp_path / "case.test.mjs").write_text(f"import {{test,expect}} from {json.dumps(module)};test('node',()=>expect(1).toBe(2));", encoding="utf-8")
    (tmp_path / "vitest.config.mjs").write_text("export default {test:{include:['case.test.mjs'],maxWorkers:1}};", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"type": "module", "scripts": {
        "check": "npm run test && npm run lint", "test": "vitest run", "lint": "node lint.mjs"}}), encoding="utf-8")
    (tmp_path / "lint.mjs").write_text("import{writeFileSync}from'node:fs';writeFileSync('lint-finished','yes');process.exit("+
                                      str(int(lint_failure))+");", encoding="utf-8")
    entry = tmp_path / ".github/scripts/run-workspace-checks.mjs"
    entry.parent.mkdir(parents=True)
    entry.write_text("import{spawnSync}from'node:child_process';process.exit(spawnSync('npm',['run','check'],{stdio:'inherit'}).status);", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(root / "node_modules/.bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(gate, "git", lambda *args: "C")
    monkeypatch.setattr(gate, "js_runner", lambda *args: {"runner": "same"})
    if lint_failure:
        with pytest.raises(ValueError, match="harness"):
            gate.collect_js(tmp_path / "evidence")
    else:
        assert gate.collect_js(tmp_path / "evidence") == 1
    assert (tmp_path / "lint-finished").read_text(encoding="utf-8") == "yes"
