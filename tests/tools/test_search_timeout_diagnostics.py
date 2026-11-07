"""Find diagnostics never become content-search payload; tiny budgets never spawn."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import file_operations_search
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


@pytest.mark.platforms("posix")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses permissions")
@pytest.mark.parametrize("mode", ["files_only", "content", "count"])
@pytest.mark.parametrize("has_match", [False, True], ids=["diagnostic-only", "partial-match"])
def test_find_permission_diagnostic_is_not_payload(tmp_path, mode, has_match):
    root = tmp_path / ".selected" / "run-2"
    locked = root / "locked"
    locked.mkdir(parents=True)
    readable = root / "readable.txt"
    if has_match:
        readable.write_text("needle\n", encoding="utf-8")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))
    locked.chmod(0)
    try:
        result = ops._search_with_grep("needle", str(root), None, 50, 0, mode, 0)
    finally:
        locked.chmod(0o700)

    assert all(not path.startswith("find: ") for path in result.files), result.to_dict()
    assert result.error is None, result.to_dict()
    assert result.warning and "restricted permissions" in result.warning, result.to_dict()
    assert result.skipped_paths == [str(locked)]
    if not has_match:
        assert result.total_count == 0
    else:
        assert result.total_count == 1, result.to_dict()
        if mode == "files_only":
            assert result.files == [str(readable)]
        elif mode == "count":
            assert result.counts == {str(readable): 1}
        else:
            assert [match.path for match in result.matches] == [str(readable)]


@pytest.mark.parametrize("phase", ["supplement", "rg"])
def test_subsecond_filename_budget_does_not_spawn(tmp_path, monkeypatch, phase):
    ops = ShellFileOperations(SimpleNamespace(cwd=str(tmp_path), is_local=False))
    monkeypatch.setattr(file_operations_search, "time", SimpleNamespace(monotonic=lambda: 59.6))
    spawn = Mock(side_effect=AssertionError("less than one SDK second remains"))
    monkeypatch.setattr(ops, "_exec", spawn)
    monkeypatch.setattr(ops, "_run_rg_bounded", spawn)

    if phase == "supplement":
        result = ops._exec_before_deadline("find .", 60.0)
        assert result.exit_code == 124
    else:
        result = ops._search_filename_root("*.py", str(tmp_path), ["*.py"], ".", 50,
                                           "discovery", "rg", 60.0, False)
        assert result.limit_reason == "search_timeout"
    spawn.assert_not_called()
