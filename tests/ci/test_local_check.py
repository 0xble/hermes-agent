from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.ci import local_check as MODULE


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "scripts" / "ci" / "local_check.py"


def names(checks):
    return [check.name for check in checks]


def test_smoke_uses_only_stdlib_python_and_portability_guard() -> None:
    checks = MODULE.build_checks(ROOT, "smoke", ["scripts/ci/local_check.py"], [])
    assert names(checks) == ["compile changed Python", "Windows portability guard"]
    assert all(not check.remote_only for check in checks)
    assert not any("uv" in check.command or "npm" in check.command for check in checks)


def test_python_compilation_does_not_write_bytecode(tmp_path: Path) -> None:
    source = tmp_path / "changed.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    compile_check = MODULE.build_checks(tmp_path, "smoke", [source.name], [])[0]

    results = MODULE.run_checks(tmp_path, [compile_check], dry_run=False)

    assert results[0].status == "passed"
    assert not (tmp_path / "__pycache__").exists()


def test_python_compilation_is_batched_for_large_changes(tmp_path: Path) -> None:
    paths = [f"pkg/module_{index}.py" for index in range(401)]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    checks = MODULE.build_checks(tmp_path, "smoke", paths, [])
    compile_checks = [check for check in checks if check.name.startswith("compile changed Python")]
    assert len(compile_checks) == 3
    assert all(len(check.command) <= 203 for check in compile_checks)


def test_deleted_python_file_is_not_compiled(tmp_path: Path) -> None:
    checks = MODULE.build_checks(tmp_path, "smoke", ["deleted.py"], [])
    assert not any(check.name.startswith("compile changed Python") for check in checks)


def test_changed_files_includes_deleted_and_renamed_paths(
    monkeypatch, tmp_path: Path
) -> None:
    captured: list[tuple[str, ...]] = []

    def fake_git(_root: Path, *args: str) -> str:
        captured.append(args)
        return "D\0tools/deleted_tool.py\0R100\0pyproject.toml\0docs/pyproject.md\0C100\0copy.py\0copied.py\0"

    monkeypatch.setattr(MODULE, "_git", fake_git)

    assert MODULE.changed_files(tmp_path, "origin/main", "HEAD") == [
        "tools/deleted_tool.py",
        "pyproject.toml",
        "docs/pyproject.md",
        "copy.py",
        "copied.py",
    ]
    assert captured == [
        (
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies",
            "--diff-filter=ACMRDT",
            "origin/main...HEAD",
        )
    ]


@pytest.mark.parametrize("filename", ["café.py", "tab\tcode.py", "line\ncode.py"])
def test_quoted_git_paths_reach_actual_compile_check(tmp_path, filename):
    if sys.platform == "win32" and any(c in filename for c in "\t\n"):
        pytest.skip("Windows filenames cannot contain control characters")
    def git(*args):
        return subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
                              cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (tmp_path / "original.py").write_text("answer = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "original.py").rename(tmp_path / filename)
    git("add", "-A")
    git("commit", "-qm", "rename")
    assert MODULE.changed_files(tmp_path, base, "HEAD") == ["original.py", filename]
    (tmp_path / filename).write_text("def broken(:\n")
    git("add", "-A")
    git("commit", "-qm", "syntax error")
    paths = MODULE.changed_files(tmp_path, "HEAD^", "HEAD")
    assert paths == [filename]
    checks = [c for c in MODULE.build_checks(tmp_path, "smoke", paths, []) if c.name.startswith("compile changed Python")]
    assert len(checks) == 1
    assert MODULE.run_checks(tmp_path, checks, dry_run=False)[0].status == "failed"


def test_documentation_build_uses_website_prefix() -> None:
    checks = MODULE.build_checks(ROOT, "affected", ["website/docs/example.md"], [])
    dependencies = next(
        check for check in checks if check.name == "Documentation dependencies"
    )
    documentation = next(check for check in checks if check.name == "Documentation site")
    assert dependencies.command == ("npm", "--prefix", "website", "ci")
    assert documentation.command == (
        "npm",
        "--prefix",
        "website",
        "run",
        "build:fast",
    )


def test_affected_python_defaults_to_full_suite_when_targets_are_not_explicit(monkeypatch) -> None:
    # POSIX has no shell prefix; the Windows shape is covered by the win32 tests below.
    monkeypatch.setattr(MODULE.sys, "platform", "linux")
    checks = MODULE.build_checks(ROOT, "affected", ["scripts/ci/local_check.py"], [])
    python = next(check for check in checks if check.name == "Python tests")
    assert python.command == ("scripts/run_tests.sh",)


def test_affected_python_preserves_explicit_focused_targets(monkeypatch) -> None:
    monkeypatch.setattr(MODULE.sys, "platform", "linux")
    checks = MODULE.build_checks(
        ROOT,
        "affected",
        ["scripts/ci/local_check.py"],
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

    desktop = [check for check in checks if check.name.startswith("Desktop")]
    javascript = next(check for check in checks if check.name == "JS and TS workspace checks")
    assert len(desktop) == 2
    assert all(check.remote_only for check in desktop)
    assert "apps/desktop::check:test:desktop:platforms" in javascript.command
    assert "apps/desktop::check:test:desktop:all" in javascript.command


def test_json_dry_run_binds_receipt_to_exact_head() -> None:
    completed = subprocess.run(
        [
            sys.executable,
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
    assert payload["duration_seconds"] >= 0
    assert "worktree_dirty_after" in payload
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


def test_requested_head_must_be_the_tested_checkout(tmp_path, capsys):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    git('commit', '--allow-empty', '-qm', 'first')
    old = git('rev-parse', 'HEAD')
    git('commit', '--allow-empty', '-qm', 'second')
    assert MODULE.main(['--profile', 'smoke', '--repo-root', str(tmp_path),
                        '--head', old, '--dry-run', '--json']) == 2
    assert 'differs from the checkout' in json.loads(capsys.readouterr().out)['error']


@pytest.fixture
def git_repo(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    (tmp_path / "code.py").write_text("answer = 1\n")
    (tmp_path / ".gitignore").write_text("cache/\n")
    git("add", ".")
    git("commit", "-qm", "base")
    return tmp_path, git


def test_type_change_selects_python_and_reaches_compile(git_repo):
    root, git = git_repo
    base = git("rev-parse", "HEAD")
    (root / "invalid.txt").write_text("def broken(:\n")
    (root / "code.py").unlink()
    try:
        (root / "code.py").symlink_to("invalid.txt")
    except OSError:
        pytest.skip("symlink creation unavailable")
    (root / "readme.md").write_text("docs")
    git("add", "-A")
    git("commit", "-qm", "type change and docs")
    paths = MODULE.changed_files(root, base, "HEAD")
    assert "code.py" in paths
    assert "Python tests" in names(MODULE.build_checks(root, "affected", paths, []))
    compile_checks = [c for c in MODULE.build_checks(root, "smoke", paths, [])
                      if c.name.startswith("compile changed Python")]
    assert MODULE.run_checks(root, compile_checks, False)[0].status == "failed"
    base = git("rev-parse", "HEAD")
    (root / "code.py").unlink()
    (root / "code.py").write_text("answer = 2\n")
    git("add", "-A")
    git("commit", "-qm", "back to regular")
    assert "code.py" in MODULE.changed_files(root, base, "HEAD")


@pytest.mark.parametrize("mutation", ["dirty", "untracked", "index", "hidden", "mode", "delete", "create", "symlink"])
def test_mutation_receipt_binds_contents_and_index(git_repo, monkeypatch, capsys, mutation):
    import os
    root, git = git_repo
    source = root / "code.py"
    source.write_text("answer = 2\n")
    (root / "untracked.txt").write_text("before")
    if mutation == "index":
        git("add", "code.py")
        source.write_text("answer = 3\n")
    if mutation == "hidden":
        git("add", "code.py")
        git("commit", "-qm", "tracked")
        git("update-index", "--assume-unchanged", "code.py")
    if mutation == "symlink":
        try:
            (root / "link").symlink_to("before")
        except OSError:
            pytest.skip("symlink creation unavailable")
    def mutate(*args):
        if mutation in {"dirty", "hidden"}:
            stamp = source.stat()
            source.write_text("answer = 9\n")
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        elif mutation == "untracked":
            (root / "untracked.txt").write_text("after!")
        elif mutation == "index":
            source.write_text("answer = 4\n")
            git("add", "code.py")
            source.write_text("answer = 3\n")
        elif mutation == "mode":
            source.chmod(0o755)
        elif mutation == "delete":
            source.unlink()
        elif mutation == "create":
            (root / "new.txt").write_text("new")
        else:
            (root / "link").unlink()
            (root / "link").symlink_to("after")
        return []
    monkeypatch.setattr(MODULE, "run_checks", mutate)
    assert MODULE.main(["--profile", "fast", "--repo-root", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert any(r["name"] == "Worktree mutation guard" for r in payload["results"])


def test_unchanged_dirty_tree_and_ignored_cache_are_allowed(git_repo, monkeypatch, capsys):
    root, git = git_repo
    (root / "code.py").write_text("answer = 2\n")
    (root / "untracked.txt").write_text("keep")
    def create_cache(*args):
        (root / "cache").mkdir()
        (root / "cache" / "generated").write_text("generated")
        return []
    monkeypatch.setattr(MODULE, "run_checks", create_cache)
    assert MODULE.main(["--profile", "fast", "--repo-root", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


@pytest.mark.parametrize("profile", ["full", "affected"])
def test_windows_python_checks_use_explicit_supported_shell(monkeypatch, profile):
    monkeypatch.setattr(MODULE.sys, "platform", "win32")
    monkeypatch.setattr(MODULE, "_windows_bash", lambda: "C:/Program Files/Git/bin/bash.exe", raising=False)
    checks = MODULE.build_checks(ROOT, profile, ["code.py"], ["tests/with space.py"])
    check = next(c for c in checks if c.name == "Python tests")
    assert check.command[:2] == ("C:/Program Files/Git/bin/bash.exe", "scripts/run_tests.sh")
    if profile == "affected":
        assert check.command[2:] == ("tests/with space.py",)


@pytest.mark.parametrize("name", [" leading.txt", "tab\tname", "line\nname"])
def test_fingerprint_preserves_untracked_git_path_bytes(git_repo, name):
    if sys.platform == "win32" and any(c in name for c in "\t\n"):
        pytest.skip("Windows filenames exclude control characters")
    root, _ = git_repo
    path = root / name
    path.write_text("before")
    before = MODULE.worktree_fingerprint(root)
    path.write_text("after!")
    assert before != MODULE.worktree_fingerprint(root)


def test_fingerprint_detects_index_flag_changes(git_repo):
    root, git = git_repo
    before = MODULE.worktree_fingerprint(root)
    git("update-index", "--assume-unchanged", "code.py")
    assert before != MODULE.worktree_fingerprint(root)


def test_windows_bash_discovery_rejects_wsl_and_finds_git(tmp_path, monkeypatch):
    wsl = tmp_path / "Windows" / "System32" / "bash.exe"
    wsl.parent.mkdir(parents=True)
    wsl.touch()
    git = tmp_path / "Git" / "cmd" / "git.exe"
    git.parent.mkdir(parents=True)
    git.touch()
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: str(git if name == "git" else wsl))
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="Git Bash"):
        MODULE._windows_bash()
    bash = git.parent.parent / "bin" / "bash.exe"
    bash.parent.mkdir()
    bash.touch()
    bash.with_name("sh.exe").touch()
    assert MODULE._windows_bash() == str(bash)


def test_explicit_shell_executes_canonical_script_and_preserves_arguments(tmp_path, monkeypatch):
    import shutil
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable for local dispatch proof")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "run_tests.sh").write_text('printf "%s\\n" "$@"\n')
    monkeypatch.setattr(MODULE.sys, "platform", "win32")
    monkeypatch.setattr(MODULE, "_windows_bash", lambda: bash)
    command = MODULE._python_test_command(("tests/path with spaces.py", "-q"))
    result = MODULE.run_checks(tmp_path, [MODULE.Check("Python tests", command)], False)[0]
    assert result.status == "passed"
    assert result.stdout.splitlines() == ["tests/path with spaces.py", "-q"]
