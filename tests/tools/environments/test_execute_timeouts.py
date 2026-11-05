"""SDK-facing execution timeouts are whole seconds, including deadline budgets."""

import os
import shutil
import subprocess

import pytest

from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _ThreadedProcessHandle
from tools.file_operations import ShellFileOperations


class _IntegerTimeoutEnvironment(BaseEnvironment):
    """Run real shell commands behind the same strict/threaded seam as SDK backends."""

    is_local = False

    def __init__(self, root):
        super().__init__(cwd=str(root), timeout=10, persist_env=False)
        self._cwd_file = str(root / "cwd.txt")
        self._prefer_nonlogin = True
        self.calls = []

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        self.calls.append((cmd_string, timeout))
        bash = shutil.which("bash")
        assert bash is not None

        def execute():
            if type(timeout) is not int:
                raise TypeError("SDK timeout must be an integer")
            result = subprocess.run(
                [bash, "-c", cmd_string], input=stdin_data,
                capture_output=True, text=True, cwd=self.cwd, timeout=timeout,
            )
            return result.stdout + result.stderr, result.returncode

        return _ThreadedProcessHandle(execute)

    def cleanup(self):
        pass


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("timeout,expected", [(59.73, 59), (0.4, 1), (7, 7), (None, 10)])
def test_execute_normalizes_sdk_timeout(tmp_path, timeout, expected):
    env = _IntegerTimeoutEnvironment(tmp_path)

    result = env.execute("printf 'executed'", timeout=timeout)

    assert result["returncode"] == 0, result
    assert result["output"] == "executed"
    assert env.calls[-1][1] == expected
    assert type(env.calls[-1][1]) is int


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("order", ["discovery", "modified"])
def test_filename_search_on_integer_only_backend(tmp_path, order):
    if not shutil.which("rg") or not shutil.which("git"):
        pytest.skip("requires ripgrep and git")
    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    ignored = root / "ignored.py"
    ignored.mkdir()
    directory = root / "directory.py"
    directory.mkdir()
    old = root / "old.py"
    new = other / "new.py"
    old.write_text("old\n", encoding="utf-8")
    new.write_text("new\n", encoding="utf-8")
    for entry, mtime in [(old, 1_000), (directory, 2_000), (new, 3_000)]:
        os.utime(entry, (mtime, mtime))
    env = _IntegerTimeoutEnvironment(tmp_path)
    ops = ShellFileOperations(env)

    result = ops.search("*.py", path=f"{root}, {other}", target="files", order=order)

    assert result.error is None, result.to_dict()
    assert result.truncated is False, result.to_dict()
    assert set(result.files) == {str(old), str(directory), str(new)}, result.to_dict()
    if order == "modified":
        assert result.files == [str(new), str(directory), str(old)]
        assert any("while IFS= read" in command for command, _ in env.calls)
    assert all(type(timeout) is int for _, timeout in env.calls)
