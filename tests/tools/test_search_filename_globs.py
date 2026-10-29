"""Filename globs retain brace/path syntax and engine failures stay out of paths."""

import shlex
import shutil
import sys
from pathlib import Path

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture(scope="module")
def local_env(tmp_path_factory):
    return LocalEnvironment(cwd=str(tmp_path_factory.mktemp("filename-globs")))


@pytest.fixture(params=["0", "1"], ids=["shell", "native"])
def search(tmp_path, local_env, monkeypatch, request):
    rg = shutil.which("rg")
    if not rg:
        pytest.skip("filename glob integration requires ripgrep")
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", request.param)
    local_env.cwd = str(tmp_path)
    ops = ShellFileOperations(local_env, cwd=str(tmp_path))
    resolve = ops._resolve_command
    monkeypatch.setattr(ops, "_resolve_command", lambda cmd: rg if cmd == "rg" else resolve(cmd))
    for name in (
        "lefthook.yml", "sub/lefthook.yaml", ".lefthook.yml",
        "hooks/pre-commit", "elsewhere/pre-commit", "sub/main.py", "other.txt",
    ):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture\n")

    def run(pattern, **kwargs):
        return ops.search(pattern, str(tmp_path), target="files", **kwargs)

    return run, tmp_path, ops


@pytest.mark.parametrize("pattern,expected", [
    (
        "{lefthook.yml,lefthook.yaml,.lefthook.yml,.lefthook.yaml,.githooks/*}",
        # Explicit globs select dotfiles, but rg still prunes hidden directories.
        {"lefthook.yml", "sub/lefthook.yaml", ".lefthook.yml"},
    ),
    ("*.py", {"sub/main.py"}),
    ("lefthook.yml", {"lefthook.yml", ".lefthook.yml"}),
    ("hooks/*", {"hooks/pre-commit"}),
    ("**/hooks/*", {"hooks/pre-commit"}),
    ("{hooks,elsewhere}/pre-*", {"hooks/pre-commit", "elsewhere/pre-commit"}),
])
def test_filename_globs_preserve_alternatives_paths_and_hidden_default(search, pattern, expected):
    run, root, _ = search
    if ".githooks/" in pattern:
        (root / ".githooks").mkdir()
        (root / ".githooks/pre-commit").write_text("hook\n")
    result = run(pattern)
    assert result.error is None, result.to_dict()
    assert {Path(file).relative_to(root).as_posix() for file in result.files} == expected


@pytest.mark.parametrize("order", ["discovery", "modified"])
def test_filename_failures_report_bounded_diagnostics_separate_from_paths(search, order):
    run, _, ops = search
    result = run("*}", order=order)
    payload = result.to_dict()
    assert not result.files
    assert payload["error"]
    assert "exit 2" in payload["error"]
    assert "error parsing glob" in payload["error"]
    assert "unopened alternate group" in payload["error"]
    assert len(payload["error"]) < 4600

    # Drain noisy failures past the capture cap without deadlock or path pollution.
    script = (
        "import sys; sys.stderr.write('rg diagnostic ' + 'x' * 100000); "
        "sys.stderr.flush(); print('only-path.py'); sys.exit(2)"
    )
    transport = ops._run_rg_bounded(
        [shlex.quote(sys.executable), "-c", shlex.quote(script)],
        10, timeout=10, capture_stderr=True, shell_prefix="set -o pipefail; ",
    )
    assert transport.exit_code == 2
    assert transport.stdout == "only-path.py\n"
    assert transport.stderr.startswith("rg diagnostic ")
    assert len(transport.stderr) == 4096
