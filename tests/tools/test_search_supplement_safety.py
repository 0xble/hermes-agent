"""Directory discovery shares filename search's pruning and wall-clock budget."""

import shutil
from types import SimpleNamespace

import pytest

from tools import file_operations, file_operations_search
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("native", ["0", "1"], ids=["shell", "native"])
@pytest.mark.parametrize("multi_root", [False, True], ids=["single-root", "multi-root"])
def test_directory_supplement_prunes_protected_entries(tmp_path, monkeypatch, native, multi_root):
    rg = shutil.which("rg")
    if not rg:
        pytest.skip("requires ripgrep")
    home = tmp_path / "home[alice]"
    protected = home / "Downloads" / "secret-tests"
    visible = home / "work" / "Downloads" / "visible-tests"
    other = tmp_path / "other"
    for directory in (protected, visible, other):
        directory.mkdir(parents=True)
    monkeypatch.setattr(file_operations, "_HOME", str(home))
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))
    resolve = ops._resolve_command
    monkeypatch.setattr(ops, "_resolve_command", lambda cmd: rg if cmd == "rg" else resolve(cmd))
    path = f"{home}, {other}" if multi_root else str(home)

    result = ops.search("*tests", path=path, target="files")

    assert result.error is None, result.to_dict()
    assert result.files == [str(visible)], result.to_dict()
    assert "Skipped macOS protected folders" in (result.warning or "")
    explicit = ops.search("*tests", path=str(home / "Downloads"), target="files")
    assert explicit.error is None, explicit.to_dict()
    assert explicit.files == [str(protected)], explicit.to_dict()


@pytest.mark.platforms("posix")
def test_grep_error_under_explicit_hidden_root_is_not_silent(tmp_path):
    hidden = tmp_path / ".selected"
    hidden.mkdir()
    (hidden / "readable.txt").write_text("needle\n", encoding="utf-8")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))

    error = ops._search_with_grep("[", str(hidden), None, 50, 0, "content", 0)
    empty = ops._search_with_grep("absent", str(hidden), None, 50, 0, "content", 0)

    assert error.error, error.to_dict()
    assert error.matches == []
    assert empty.error is None, empty.to_dict()
    assert empty.total_count == 0


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("native", ["0", "1"], ids=["shell", "native"])
@pytest.mark.parametrize("phase", ["find", "bsd-find", "root-stat", "global-stat"])
@pytest.mark.parametrize("budget", [0.25, 1.25], ids=["subsecond-skip", "remaining-budget"])
def test_filename_deadline_covers_supplement_and_metadata(tmp_path, monkeypatch, native, phase, budget):
    rg = shutil.which("rg")
    if not rg:
        pytest.skip("requires ripgrep")
    root = tmp_path / "root"
    root.mkdir()
    for name in ("a-tests", "b-tests"):
        (root / name).write_text("fixture\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))
    resolve = ops._resolve_command
    monkeypatch.setattr(ops, "_resolve_command", lambda cmd: rg if cmd == "rg" else resolve(cmd))
    elapsed = [0.0]
    monkeypatch.setattr(file_operations_search, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    real_rg = ops._run_rg_bounded
    calls = []

    def run_rg(*args, **kwargs):
        result = real_rg(*args, **kwargs)
        # Leave only the shared remainder for the supplement/stat pass.
        elapsed[0] = 60 - budget
        return result

    real_exec = ops._exec

    def execute(command, **kwargs):
        if command.startswith("find "):
            calls.append(("find", kwargs["timeout"]))
            if phase == "bsd-find" and "-printf" in command:
                return file_operations.ExecuteResult("", 1)
            if phase in {"find", "bsd-find"}:
                # A fake slow find: virtual work consumes up to the supplied timeout.
                elapsed[0] += min(2.0, kwargs["timeout"])
                if kwargs["timeout"] < 2:
                    return file_operations.ExecuteResult(
                        f"[Command timed out after {kwargs['timeout']}s]", 124)
                return file_operations.ExecuteResult("", 0)
            return file_operations.ExecuteResult(f"{root}/empty-tests\n", 0)
        if command.startswith("while IFS= read"):
            calls.append(("stat", kwargs["timeout"]))
            elapsed[0] += min(2.0, kwargs["timeout"])
            return file_operations.ExecuteResult("", 124 if kwargs["timeout"] < 2 else 0)
        return real_exec(command, **kwargs)

    monkeypatch.setattr(ops, "_run_rg_bounded", run_rg)
    monkeypatch.setattr(ops, "_exec", execute)
    if phase == "global-stat":
        monkeypatch.setattr(ops, "_has_command", lambda cmd: cmd == "rg")
    result = ops.search("*tests", path=str(root), target="files",
                        order="modified" if phase == "global-stat" else "discovery")

    assert result.error is None, result.to_dict()
    assert result.truncated is True, result.to_dict()
    assert result.limit_reason == "search_timeout"
    assert all("[Command timed out" not in file for file in result.files)
    assert {str(root / "a-tests"), str(root / "b-tests")} <= set(result.files)
    if budget < 1:
        # No SDK-compatible command fits in the final fractional second.
        assert calls == [], calls
        assert elapsed[0] == 60 - budget
    else:
        assert calls and all(0 < timeout <= budget for _, timeout in calls), calls
        assert elapsed[0] <= 60, calls
