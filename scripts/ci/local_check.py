#!/usr/bin/env python3
"""Run repository-owned local CI profiles without mutating the checkout."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPILE_CHANGED_PYTHON = (
    "import pathlib,sys; "
    "[compile(pathlib.Path(path).read_bytes(), path, 'exec') for path in sys.argv[1:]]"
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ci.classify_changes import classify


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    remote_only: bool = False


@dataclass
class Result:
    name: str
    command: list[str]
    status: str
    returncode: int | None
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def changed_files(root: Path, base: str | None, head: str) -> list[str]:
    """Return immutable base...head paths; broad profiles need no path payload."""
    if base:
        output = _git(
            root,
            "diff",
            "--name-status",
            "--find-renames",
            "--find-copies",
            "--diff-filter=ACMRD",
            f"{base}...{head}",
        )
        paths: list[str] = []
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            status = fields[0]
            if status.startswith(("R", "C")) and len(fields) >= 3:
                paths.extend(fields[1:3])
            else:
                paths.append(fields[1])
        return list(dict.fromkeys(paths))
    return []


def _python_files(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(path for path in paths if path.endswith(".py"))


def _chunks(values: Sequence[str], size: int = 200) -> list[tuple[str, ...]]:
    return [tuple(values[index : index + size]) for index in range(0, len(values), size)]


def validate_worktree(profile: str, dirty: bool, allow_dirty: bool) -> None:
    if profile == "full" and dirty and not allow_dirty:
        raise ValueError("--profile full requires a clean worktree (or explicit --allow-dirty)")


def project_python(root: Path) -> str:
    for candidate in (
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def build_checks(
    root: Path,
    profile: str,
    paths: Sequence[str],
    python_tests: Sequence[str],
) -> list[Check]:
    """Build an explicit, non-mutating check list for a profile."""
    python = project_python(root)
    py_files = tuple(
        path for path in _python_files(paths) if (root / path).is_file()
    )
    checks: list[Check] = []
    for index, chunk in enumerate(_chunks(py_files), start=1):
        suffix = f" ({index}/{math.ceil(len(py_files) / 200)})" if len(py_files) > 200 else ""
        checks.append(
            Check(
                f"compile changed Python{suffix}",
                (python, "-c", _COMPILE_CHANGED_PYTHON, *chunk),
            )
        )
    checks.append(
        Check(
            "Windows portability guard",
            (python, "scripts/check-windows-footguns.py", "--all"),
        )
    )
    if profile == "smoke":
        return checks

    checks.append(Check("Ruff", ("uv", "tool", "run", "ruff", "check", ".")))
    if profile == "fast":
        return checks

    lanes = classify(list(paths))
    if profile == "affected":
        if lanes["python"]:
            targets = tuple(python_tests) if python_tests else ()
            checks.append(Check("Python tests", ("scripts/run_tests.sh", *targets)))
        if lanes["frontend"]:
            checks.append(
                Check(
                    "JS and TS workspace checks",
                    (
                        "node",
                        ".github/scripts/run-workspace-checks.mjs",
                        "--exclude",
                        "apps/desktop::check:test:desktop:platforms",
                        "--exclude",
                        "apps/desktop::check:test:desktop:all",
                    ),
                )
            )
            checks.append(
                Check(
                    "Desktop platform tests (hosted clean-room residual)",
                    ("npm", "run", "--prefix", "apps/desktop", "check:test:desktop:platforms"),
                    remote_only=True,
                )
            )
            checks.append(
                Check(
                    "Desktop packaging tests (hosted clean-room residual)",
                    ("npm", "run", "--prefix", "apps/desktop", "check:test:desktop:all"),
                    remote_only=True,
                )
            )
        if lanes["site"]:
            checks.extend(
                [
                    Check(
                        "Documentation dependencies",
                        ("npm", "--prefix", "website", "ci"),
                    ),
                    Check(
                        "Documentation site",
                        ("npm", "--prefix", "website", "run", "build:fast"),
                    ),
                ]
            )
        if lanes["rust"]:
            checks.append(
                Check(
                    "Rust tests",
                    (
                        "cargo",
                        "test",
                        "--manifest-path",
                        "apps/bootstrap-installer/src-tauri/Cargo.toml",
                    ),
                )
            )
        if lanes["uv_lock"]:
            checks.append(Check("uv lock", ("uv", "lock", "--check")))
        if lanes["installer"]:
            for script in (
                "scripts/tests/test-install-ps1-longpath.ps1",
                "scripts/tests/test-install-ps1-node-compatibility.ps1",
            ):
                checks.append(
                    Check(
                        f"Windows installer test: {Path(script).name}",
                        ("pwsh", "-NoProfile", "-File", script),
                        remote_only=sys.platform != "win32" or shutil.which("pwsh") is None,
                    )
                )
        return checks

    checks.extend(
        [
            Check("Python tests", ("scripts/run_tests.sh",)),
            Check(
                "JS and TS workspace checks",
                (
                    "node",
                    ".github/scripts/run-workspace-checks.mjs",
                    "--exclude",
                    "apps/desktop::check:test:desktop:platforms",
                    "--exclude",
                    "apps/desktop::check:test:desktop:all",
                ),
            ),
            Check(
                "Desktop platform tests (hosted clean-room residual)",
                ("npm", "run", "--prefix", "apps/desktop", "check:test:desktop:platforms"),
                remote_only=True,
            ),
            Check(
                "Desktop packaging tests (hosted clean-room residual)",
                ("npm", "run", "--prefix", "apps/desktop", "check:test:desktop:all"),
                remote_only=True,
            ),
            Check(
                "Documentation dependencies",
                ("npm", "--prefix", "website", "ci"),
            ),
            Check(
                "Documentation site",
                ("npm", "--prefix", "website", "run", "build:fast"),
            ),
            Check(
                "Rust tests",
                (
                    "cargo",
                    "test",
                    "--manifest-path",
                    "apps/bootstrap-installer/src-tauri/Cargo.toml",
                ),
            ),
            Check("uv lock", ("uv", "lock", "--check")),
            Check(
                "Windows-only Python tests",
                (python, "-m", "pytest", "-m", "windows_only and not integration"),
                remote_only=sys.platform != "win32",
            ),
        ]
    )
    for script in (
        "scripts/tests/test-install-ps1-longpath.ps1",
        "scripts/tests/test-install-ps1-node-compatibility.ps1",
    ):
        checks.append(
            Check(
                f"Windows installer test: {Path(script).name}",
                ("pwsh", "-NoProfile", "-File", script),
                remote_only=sys.platform != "win32" or shutil.which("pwsh") is None,
            )
        )
    return checks


def _environment(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    candidate_bins = [root / ".venv" / "bin", root / "venv" / "bin"]
    env["PATH"] = os.pathsep.join(
        [str(path) for path in candidate_bins if path.is_dir()] + [env.get("PATH", "")]
    )
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY"):
        env[key] = ""
    return env


def run_checks(root: Path, checks: Sequence[Check], dry_run: bool) -> list[Result]:
    results: list[Result] = []
    env = _environment(root)
    for check in checks:
        command = list(check.command)
        if check.remote_only:
            results.append(Result(check.name, command, "remote_only", None, 0.0))
            continue
        if dry_run:
            results.append(Result(check.name, command, "planned", None, 0.0))
            continue
        start = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        result = Result(
            check.name,
            command,
            "passed" if completed.returncode == 0 else "failed",
            completed.returncode,
            round(time.monotonic() - start, 3),
            completed.stdout,
            completed.stderr,
        )
        results.append(result)
        if completed.returncode != 0:
            break
    return results


def _print_human(results: Sequence[Result]) -> None:
    for result in results:
        command = " ".join(result.command)
        print(f"[{result.status}] {result.name}: {command}")
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")


def main(argv: Sequence[str] | None = None) -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("smoke", "fast", "affected", "full"), required=True)
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--python-test", action="append", default=[])
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)

    root = args.repo_root.resolve()
    if args.profile == "affected" and not args.base:
        parser.error("--profile affected requires --base")
    try:
        head_sha = _git(root, "rev-parse", "--verify", f"{args.head}^{{commit}}")
        if _git(root, "rev-parse", "HEAD") != head_sha:
            raise ValueError("requested head differs from the checkout to be tested")
        base_sha = _git(root, "rev-parse", args.base) if args.base else None
        paths = changed_files(root, args.base, args.head)
        status_before = _git(root, "status", "--porcelain", "--untracked-files=all")
        dirty = bool(status_before)
        validate_worktree(args.profile, dirty, args.allow_dirty)
        checks = build_checks(root, args.profile, paths, args.python_test)
        results = run_checks(root, checks, args.dry_run)
        if _git(root, "rev-parse", "HEAD") != head_sha:
            raise ValueError("checkout HEAD changed during local checks")
        status_after = _git(root, "status", "--porcelain", "--untracked-files=all")
        if status_after != status_before:
            results.append(
                Result(
                    "Worktree mutation guard",
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                    "failed",
                    1,
                    0.0,
                    status_after,
                    "local checks changed the worktree; restore it before trusting this receipt\n",
                )
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if args.as_json:
            print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        else:
            print(f"local CI error: {exc}", file=sys.stderr)
        return 2

    failed = any(result.status == "failed" for result in results)
    payload = {
        "profile": args.profile,
        "base": base_sha,
        "head": head_sha,
        "worktree_dirty": dirty,
        "worktree_dirty_after": bool(status_after),
        "changed_files": paths,
        "results": [asdict(result) for result in results],
        "duration_seconds": round(time.monotonic() - started, 3),
        "status": "failed" if failed else "passed",
    }
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(results)
        remote = [result.name for result in results if result.status == "remote_only"]
        if remote:
            print(f"remote-only residuals: {', '.join(remote)}")
        print(f"local CI {payload['status']}: {args.profile} at {head_sha[:12]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
