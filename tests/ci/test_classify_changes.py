"""Tests for scripts/ci/classify_changes.py.

Check some common patterns of file modifications and the CI lanes they should run.
We should always fail open. We may run a lane we didn't need, never skip one a
change could have broken.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "classify_changes.py"
_spec = importlib.util.spec_from_file_location("classify_changes", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load classify_changes.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify
ci_review_files = _mod.ci_review_files


DEFAULT = {
    "python": True,
    "python_prod": True,
    "frontend": True,
    "docker": True,
    "docker_meta": True,
    "nix": True,
    "site": True,
    "scan": True,
    "deps": True,
    "uv_lock": True,
    "npm_lock": True,
    "installer": True,
    "desktop_updater": True,
    "rust": True,
    "mcp_catalog": False,
    "ci_review": True,
}


def _lanes(python=False, frontend=False, site=False, scan=False, deps=False, uv_lock=False, npm_lock=False, installer=False, desktop_updater=False, rust=False, mcp_catalog=False, docker_meta=False, ci_review=False, python_prod=None, nix=None, docker=None) -> dict[str, bool]:
    # python_prod tracks python except for tests-only diffs; default it to
    # python so the majority of cases don't need to spell it out.
    #
    # docker and nix are derived: both build the product, so both ride on
    # python_prod and frontend. The image ships the built web assets, and the
    # flake bundles the compiled ui-tui. Pass either explicitly to override.
    _python_prod = python if python_prod is None else python_prod
    _product = _python_prod or frontend
    return {
        "python": python,
        "python_prod": _python_prod,
        "docker": (docker_meta or _product) if docker is None else docker,
        "nix": _product if nix is None else nix,
        "frontend": frontend,
        "docker_meta": docker_meta,
        "site": site,
        "scan": scan,
        "deps": deps,
        "uv_lock": uv_lock,
        "npm_lock": npm_lock,
        "installer": installer,
        "desktop_updater": desktop_updater,
        "rust": rust,
        "mcp_catalog": mcp_catalog,
        "ci_review": ci_review,
    }


CASES = {
    "docs-only → nothing heavy": (["README.md", "docs/guide.md"], _lanes()),
    "python source → python": (["run_agent.py"], _lanes(python=True, scan=True)),
    # pyproject.toml declares the pytest markers the OS lanes select on, so it
    # also re-arms the desktop_updater integration tests (fail-open).
    "dep manifest → python": (["pyproject.toml"], _lanes(python=True, scan=True, deps=True, uv_lock=True, desktop_updater=True)),
    "uv.lock → python": (["uv.lock"], _lanes(python=True, uv_lock=True)),
    "ts package → frontend": (["apps/desktop/src/app.tsx"], _lanes(frontend=True)),
    "ui-tui → frontend": (["ui-tui/src/entry.ts"], _lanes(frontend=True)),
    # Lockfile bump shifts every TS package's tree, but not the Python suite.
    "root lockfile → frontend, not python": (["package-lock.json"], _lanes(frontend=True, npm_lock=True)),
    "nested lockfile → npm_lock": (["website/package-lock.json"], _lanes(site=True, npm_lock=True)),
    # A website file the Python suite cannot read stays site-only.
    "website config → site": (["website/docusaurus.config.ts"], _lanes(site=True)),
    # uv lock --check re-resolves against PyPI, so it must stay off for any
    # diff that can't desync the lockfile — a registry blip on a docs PR
    # otherwise shows up as a blocking "uv.lock out of sync" red X.
    "docs → no uv_lock": (
        ["website/docs/developer-guide/plugins/index.md"],
        _lanes(python=True, site=True),
    ),
    "frontend → no uv_lock": (["apps/desktop/src/store/profile.ts"], _lanes(frontend=True)),
    # The published CIMD document is asserted about by the Python suite, so a
    # lone edit there must not skip the lane that would catch a bad edit.
    "cimd document → python + site": (
        ["website/static/oauth/client-metadata.json"],
        _lanes(python=True, site=True),
    ),
    # A new docs page must reach llms.txt, and the generator that puts it there
    # has its own tests. Skipping Python on either is how the index drifted to
    # 53% coverage while every PR stayed green.
    "docs page → python + site": (
        ["website/docs/user-guide/bot-mode.md"],
        _lanes(python=True, site=True),
    ),
    "docs generator → python + site": (
        ["website/scripts/generate-llms-txt.py"],
        _lanes(python=True, scan=True, site=True),
    ),
    # SKILL.md reads like docs, but the skill-doc tests read skills/, so a
    # skill edit must still run Python.
    "skill md → python + site": (["skills/github/SKILL.md"], _lanes(python=True, site=True)),
    "dockerfile → docker meta": (["Dockerfile"], _lanes(docker_meta=True)),
    # Only the flake reads these, so they run nix alone. No Python test opens
    # them, unlike pyproject.toml and uv.lock below.
    "nix module → nix only": (["nix/homeManagerModules.nix"], _lanes(nix=True)),
    "flake.nix → nix only": (["flake.nix"], _lanes(nix=True)),
    "flake.lock → nix only": (["flake.lock"], _lanes(nix=True)),
    # A flake-only file must not mask a Python change beside it.
    "nix + python → both": (["nix/checks.nix", "agent/x.py"], _lanes(python=True, scan=True)),
    # Nine checks run the built binary, so product Python is a nix input even
    # when the diff touches no file under nix/.
    "product python → nix": (["hermes_cli/config.py"], _lanes(python=True, scan=True)),
    # tests/ is not packaged, so the built binary cannot change.
    "tests-only → no nix": (
        ["tests/agent/test_foo.py"],
        _lanes(python=True, python_prod=False, scan=True),
    ),
    # Prose cannot change the closure or the binary.
    "docs-only → no nix": (["README.md"], _lanes()),
    # install.ps1 is a shell script Python never imports, but it's also not
    # provably prose, so python stays on (fail-open) alongside the Windows lane.
    "install.ps1 → installer": (["scripts/install.ps1"], _lanes(python=True, installer=True)),
    "installer test → installer": (
        ["scripts/tests/test-install-ps1-longpath.ps1"],
        _lanes(python=True, installer=True),
    ),
    "python source alone → no installer lane": (["run_agent.py"], _lanes(python=True, scan=True)),
    # The Windows desktop-update hand-off is a PowerShell integration surface:
    # its tests spawn the real script and poll its loopback server. They run
    # when the script, the Electron side that launches it, or their own test
    # files change — not on every hermes_state.py PR.
    "windows.ps1 → desktop_updater": (
        ["scripts/desktop-update/windows.ps1"],
        _lanes(python=True, desktop_updater=True),
    ),
    # The shipped updater page is exercised by the desktop Electron suite;
    # a page-only change must run that suite as well as the server tests.
    "updater ui.html → frontend + desktop_updater": (
        ["scripts/desktop-update/ui.html"],
        _lanes(python=True, frontend=True, desktop_updater=True),
    ),
    "desktop-update test → desktop_updater": (
        ["tests/test_desktop_update_windows_progress.py"],
        _lanes(python=True, python_prod=False, scan=True, desktop_updater=True),
    ),
    "updater-process.ts → desktop_updater": (
        ["apps/desktop/electron/updater-process.ts"],
        _lanes(frontend=True, desktop_updater=True),
    ),
    "python source alone → no desktop_updater lane": (["hermes_state.py"], _lanes(python=True, scan=True)),
    # `.rs` lives under apps/, so it matches `frontend` too. That lane builds
    # TypeScript and cannot notice a Rust error — before `rust` existed it was
    # the ONLY lane a Rust change ran, and the crate's tests never executed.
    "rust source → rust": (
        ["apps/bootstrap-installer/src-tauri/src/powershell.rs"],
        _lanes(frontend=True, rust=True),
    ),
    "cargo lockfile → rust": (
        ["apps/bootstrap-installer/src-tauri/Cargo.lock"],
        _lanes(frontend=True, rust=True),
    ),
    # Non-.rs files in the crate still change what cargo builds.
    "tauri config → rust": (
        ["apps/bootstrap-installer/src-tauri/tauri.conf.json"],
        _lanes(frontend=True, rust=True),
    ),
    "ts source alone → no rust lane": (
        ["apps/bootstrap-installer/src/main.tsx"],
        _lanes(frontend=True),
    ),
    # Unknown top-level file keeps Python on rather than risk a silent skip.
    "unknown toplevel → python": (["Makefile"], _lanes(python=True)),
    "mixed docs+python → python": (["README.md", "agent/x.py"], _lanes(python=True, scan=True)),
    "mixed docs+frontend → frontend": (["README.md", "apps/x.tsx"], _lanes(frontend=True)),
    # tests-only diffs: pytest lanes stay ON, product jobs (Desktop E2E,
    # Docker) gate on python_prod and skip.
    "tests-only → python without python_prod": (
        ["tests/agent/test_foo.py"],
        _lanes(python=True, python_prod=False, scan=True),
    ),
    # conftest.py owns the _OS_MARKS skip logic, so it re-arms the
    # desktop_updater integration tests too (fail-open).
    "conftest → python + desktop_updater": (
        ["tests/conftest.py"],
        _lanes(python=True, python_prod=False, scan=True, desktop_updater=True),
    ),
    "tests + prod source → both lanes": (
        ["tests/agent/test_foo.py", "agent/x.py"],
        _lanes(python=True, scan=True),
    ),
    # Runner infrastructure is NOT tests-only — a bad runner edit can mask
    # real failures, so it keeps the conservative full lane set.
    "test runner script → python_prod stays on": (
        ["scripts/run_tests_parallel.py"],
        _lanes(python=True, scan=True, ci_review=True),
    ),
    # Supply-chain lanes
    ".pth file → scan": (["evil.pth"], _lanes(python=True, scan=True)),
    "setup.py → scan": (["setup.py"], _lanes(python=True, scan=True)),
    "mcp catalog manifest → mcp_catalog": (
        ["optional-mcps/foo/manifest.yaml"],
        _lanes(python=True, mcp_catalog=True),
    ),
    "mcp_catalog.py → mcp_catalog": (
        ["hermes_cli/mcp_catalog.py"],
        _lanes(python=True, scan=True, mcp_catalog=True),
    ),
    # CI-sensitive files require explicit review label.
    "eslint config → ci_review": (
        ["apps/desktop/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "shared eslint config → ci_review": (
        ["eslint.config.shared.mjs"],
        _lanes(python=True, ci_review=True),
    ),
    "ui-tui eslint config → ci_review": (
        ["ui-tui/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "web eslint config → ci_review": (
        ["web/eslint.config.js"],
        _lanes(frontend=True, ci_review=True),
    ),
    "shared package eslint config → ci_review": (
        ["apps/shared/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "bootstrap-installer eslint config → ci_review": (
        ["apps/bootstrap-installer/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "prettier config → ci_review": (
        [".prettierrc"],
        _lanes(python=True, ci_review=True),
    ),
    "workflow yml → ci_review (also fail-open all)": (
        [".github/workflows/typecheck.yml"],
        DEFAULT,
    ),
    "composite action → ci_review (also fail-open all)": (
        [".github/actions/retry/action.yml"],
        DEFAULT,
    ),
    # Normal desktop source doesn't trigger ci_review.
    "desktop src → no ci_review": (
        ["apps/desktop/src/app.tsx"],
        _lanes(frontend=True),
    ),
    # Fail open: CI-config / empty / blank diffs run everything.
    ".github change → all": ([".github/workflows/tests.yml"], DEFAULT),
    "action change → all": ([".github/actions/detect-changes/action.yml"], DEFAULT),
    "empty diff → all": ([], DEFAULT),
    "blank lines → all": (["", "  "], DEFAULT),
}


@pytest.mark.parametrize("files,expected", CASES.values(), ids=CASES.keys())
def test_classify(files, expected):
    actual = classify(files)
    actual.pop("risk_full")
    actual.pop("lock_scan")
    assert actual == expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("run_agent.py", False),
        ("README.md", False),
        ("pyproject.toml", True),
        ("website/package-lock.json", True),
        (".github/workflows/ci.yaml", True),
        ("scripts/ci/classify_changes.py", True),
        ("scripts/ci/local_check.py", True),
        ("scripts/ci/validate_workflow_policy.py", True),
        ("scripts/run_tests.sh", True),
        ("scripts/check-windows-footguns.py", True),
        ("scripts/run_tests_parallel.py", True),
        ("scripts/validate_maintenance_manifest.py", True),
        ("apps/desktop/src/app.tsx", True),
        ("hermes_cli/windows_ssh_runtime.py", True),
        ("hermes_cli/macos_tcc_anchor.py", True),
        ("scripts/desktop-update/windows.ps1", True),
        ("foo/platform-windows.py", True),
        (".dockerignore", True),
        ("docker-compose.yml", True),
        ("docker-compose.windows.yml", True),
        ("tools/environments/docker.py", True),
        ("scripts/install.ps1", True),
        ("scripts/platform-helper.cmd", True),
        ("agent/platform_windows.py", True),
        ("tests/test_windows_long_paths.py", True),
        ("tests/test_macos_permissions.py", True),
    ],
)
def test_risk_full_classification(path: str, expected: bool) -> None:
    assert classify([path])["risk_full"] is expected


def test_risk_full_fails_open_when_diff_is_unavailable() -> None:
    assert classify([])["risk_full"] is True


@pytest.mark.parametrize(
    "files,expected",
    [
        ([".github/workflows/ci.yaml"], False),
        (["uv.lock"], True),
        (["website/package-lock.json"], True),
        ([], True),
    ],
)
def test_lock_scan_only_fails_open_for_an_unavailable_diff(
    files: list[str], expected: bool
) -> None:
    assert classify(files)["lock_scan"] is expected


_REPO = Path(__file__).resolve().parents[2]


def _yaml(rel: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((_REPO / rel).read_text(encoding="utf-8"))


def _run_detect_changes(
    tmp_path: Path, responses: list[str], event_name: str = "pull_request"
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    """Run the composite action's actual shell with deterministic API doubles."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    response_dir = tmp_path / "responses"
    response_dir.mkdir()
    for index, response in enumerate(responses, start=1):
        (response_dir / str(index)).write_text(response, encoding="utf-8")

    calls = tmp_path / "gh-calls"
    sleeps = tmp_path / "sleeps"
    (fake_bin / "gh").write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "calls = Path(os.environ['FAKE_GH_CALLS'])\n"
        "attempt = int(calls.read_text() or '0') + 1 if calls.exists() else 1\n"
        "calls.write_text(str(attempt))\n"
        "Path(os.environ['FAKE_GH_ARGS']).write_text(' '.join(os.sys.argv[1:]))\n"
        "response = (Path(os.environ['FAKE_GH_RESPONSES']) / str(attempt)).read_text()\n"
        "if response == '__FAIL__':\n"
        "    raise SystemExit(1)\n"
        "print(response, end='')\n",
        encoding="utf-8",
    )
    (fake_bin / "sleep").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$FAKE_SLEEPS\"\n",
        encoding="utf-8",
    )
    (fake_bin / "gh").chmod(0o755)
    (fake_bin / "sleep").chmod(0o755)

    output = tmp_path / "github-output"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "EVENT_NAME": event_name,
        "REPO": "NousResearch/hermes-agent",
        "BASE_SHA": "base-sha",
        "HEAD_SHA": "head-sha",
        "GITHUB_OUTPUT": str(output),
        "FAKE_GH_CALLS": str(calls),
        "FAKE_GH_ARGS": str(tmp_path / "gh-args"),
        "FAKE_GH_RESPONSES": str(response_dir),
        "FAKE_SLEEPS": str(sleeps),
    }
    run = _yaml(".github/actions/detect-changes/action.yml")["runs"]["steps"][0]["run"]
    completed = subprocess.run(
        ["bash", "-c", run],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, output, calls, sleeps


def test_detect_changes_retries_then_classifies_with_immutable_compare(tmp_path: Path) -> None:
    completed, output, calls, sleeps = _run_detect_changes(
        tmp_path,
        ["not json", '{"files": [{"filename": "README.md"}]}'],
    )

    assert completed.returncode == 0, completed.stderr
    assert calls.read_text() == "2"
    assert sleeps.read_text().splitlines() == ["10"]
    assert "repos/NousResearch/hermes-agent/compare/base-sha...head-sha" in (
        (tmp_path / "gh-args").read_text()
    )
    assert "/pulls/" not in (tmp_path / "gh-args").read_text()
    assert "python=false" in output.read_text()
    assert "ci_review_files=[]" in output.read_text()


@pytest.mark.parametrize(
    "response",
    [
        "__FAIL__",
        "not json",
        '{"files": [' + ",".join('{"filename": "docs/%s.md"}' % i for i in range(300)) + "]}",
        '{"files": [{}]}',
    ],
    ids=["api-exhausted", "malformed", "truncated", "invalid-file"],
)
def test_detect_changes_blocks_after_exhausted_untrusted_compare(
    tmp_path: Path, response: str
) -> None:
    completed, output, calls, sleeps = _run_detect_changes(tmp_path, [response] * 3)

    assert completed.returncode != 0
    assert calls.read_text() == "3"
    assert sleeps.read_text().splitlines() == ["10", "10"]
    assert not output.exists(), "the classifier must not emit lane outputs after compare failure"


def test_detect_changes_accepts_a_valid_empty_immutable_diff(tmp_path: Path) -> None:
    completed, output, calls, sleeps = _run_detect_changes(tmp_path, ['{"files": []}'])

    assert completed.returncode == 0, completed.stderr
    assert calls.read_text() == "1"
    assert not sleeps.exists()
    assert "python=true" in output.read_text()
    assert "risk_full=true" in output.read_text()


@pytest.mark.parametrize("event_name", ["workflow_dispatch", "schedule"])
def test_detect_changes_keeps_explicit_non_compare_events_broad(
    tmp_path: Path, event_name: str
) -> None:
    completed, output, calls, sleeps = _run_detect_changes(tmp_path, [], event_name)

    assert completed.returncode == 0, completed.stderr
    assert not calls.exists()
    assert not sleeps.exists()
    assert "python=true" in output.read_text()
    assert "risk_full=true" in output.read_text()


def test_every_lane_reaches_the_composite_action():
    """The action is the one surface every consumer reads, so it must carry all
    of them — ci.yaml, nix.yml and docker.yml each re-export a different subset.
    """
    lanes = set(classify(["run_agent.py"]))
    action_outputs = set(_yaml(".github/actions/detect-changes/action.yml")["outputs"])
    assert lanes - action_outputs == set(), "lane(s) missing from the composite action's outputs"


def test_ci_jobs_only_gate_on_smoke_outputs_that_smoke_actually_declares():
    """An ``if`` that reads an undeclared output resolves to the empty string.

    The lane then reports "skipping" on every PR, forever, and nothing goes red
    — there is no error for referencing an output a job never declared. That is
    exactly how the ``rust`` lane shipped dead: the classifier emitted it and
    the composite action re-exported it, but ci.yaml's ``detect`` job did not,
    so ``needs.detect.outputs.rust`` was never anything but "".
    """
    ci = _yaml(".github/workflows/ci.yaml")
    declared = set(ci["jobs"]["smoke"]["outputs"])

    referenced: set[str] = set()
    for job in ci["jobs"].values():
        for expr in _iter_if_expressions(job):
            referenced.update(re.findall(r"needs\.smoke\.outputs\.(\w+)", expr))

    assert referenced, "found no smoke-gated jobs — the walk is broken, not the wiring"
    assert referenced - declared == set(), "job(s) gate on an output smoke never declares"


def _iter_if_expressions(job: object):
    """Yield every ``if:`` string in a job, including inside its steps."""
    if not isinstance(job, dict):
        return
    if isinstance(cond := job.get("if"), str):
        yield cond
    for step in job.get("steps", []) or []:
        if isinstance(step, dict) and isinstance(cond := step.get("if"), str):
            yield cond


def test_ci_review_files_returns_only_sensitive_paths_sorted_and_unique():
    assert ci_review_files([
        "apps/desktop/src/app.tsx",
        ".github/workflows/ci.yml",
        "apps/desktop/eslint.config.mjs",
        ".github/workflows/ci.yml",
    ]) == [
        ".github/workflows/ci.yml",
        "apps/desktop/eslint.config.mjs",
    ]
