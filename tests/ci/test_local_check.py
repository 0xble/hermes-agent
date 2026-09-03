from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts.ci import local_check as MODULE


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "scripts" / "ci" / "local_check.py"


def names(checks):
    return [check.name for check in checks]


def test_smoke_uses_only_stdlib_python_and_portability_guard() -> None:
    checks = MODULE.build_checks(ROOT, "smoke", ["agent/example.py"], [])
    assert names(checks) == ["compile changed Python", "Windows portability guard"]
    assert all(not check.remote_only for check in checks)
    assert not any("uv" in check.command or "npm" in check.command for check in checks)


def test_python_compilation_is_batched_for_large_changes() -> None:
    paths = [f"pkg/module_{index}.py" for index in range(401)]
    checks = MODULE.build_checks(ROOT, "smoke", paths, [])
    compile_checks = [check for check in checks if check.name.startswith("compile changed Python")]
    assert len(compile_checks) == 3
    assert all(len(check.command) <= 203 for check in compile_checks)


def test_affected_python_defaults_to_full_suite_when_targets_are_not_explicit() -> None:
    checks = MODULE.build_checks(ROOT, "affected", ["agent/example.py"], [])
    python = next(check for check in checks if check.name == "Python tests")
    assert python.command == ("scripts/run_tests.sh",)


def test_affected_python_preserves_explicit_focused_targets() -> None:
    checks = MODULE.build_checks(
        ROOT,
        "affected",
        ["agent/example.py"],
        ["tests/agent/test_example.py", "tests/test_example.py"],
    )
    python = next(check for check in checks if check.name == "Python tests")
    assert python.command == (
        "scripts/run_tests.sh",
        "tests/agent/test_example.py",
        "tests/test_example.py",
    )


def test_full_marks_windows_native_work_as_remote_on_non_windows() -> None:
    checks = MODULE.build_checks(ROOT, "full", ["agent/example.py"], [])
    windows = [check for check in checks if check.name.startswith("Windows")]
    assert windows
    if MODULE.sys.platform != "win32":
        assert all(check.remote_only for check in windows if "portability" not in check.name)


def test_json_dry_run_binds_receipt_to_exact_head() -> None:
    completed = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv").exists() else "python3",
            str(PATH),
            "--profile",
            "smoke",
            "--base",
            "HEAD",
            "--dry-run",
            "--json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload = json.loads(completed.stdout)
    assert payload["status"] == "passed"
    assert len(payload["head"]) == 40
    assert len(payload["base"]) == 40
    assert payload["results"]
    assert {result["status"] for result in payload["results"]} <= {"planned", "remote_only"}


def test_full_without_base_does_not_embed_the_tracked_file_inventory() -> None:
    assert MODULE.changed_files(ROOT, None, "HEAD") == []


def test_full_receipt_requires_clean_tree() -> None:
    try:
        MODULE.validate_worktree("full", dirty=True, allow_dirty=False)
    except ValueError as exc:
        assert "clean worktree" in str(exc)
    else:
        raise AssertionError("dirty full run was accepted")

    MODULE.validate_worktree("fast", dirty=True, allow_dirty=False)
    MODULE.validate_worktree("full", dirty=True, allow_dirty=True)


def test_project_python_prefers_repository_environment(tmp_path: Path) -> None:
    candidate = tmp_path / ".venv" / "bin" / "python"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("", encoding="utf-8")
    assert MODULE.project_python(tmp_path) == str(candidate)
