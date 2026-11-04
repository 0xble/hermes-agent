"""File operations must not relocate a shared terminal session."""

import os
import shutil
from pathlib import Path

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.file_operations_search import _filename_search_coordinates

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize("native", ["0", "1"], ids=["shell", "native"])
@pytest.mark.parametrize("first", ["files", "directories", "invalid-glob"])
def test_search_preserves_cwd_across_relative_operations(tmp_path, monkeypatch, native, first):
    rg = shutil.which("rg")
    if not rg:
        pytest.skip("requires ripgrep")
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
    root = tmp_path.resolve()
    (root / "sub").mkdir()
    (root / "sub/main.py").write_text("needle\n", encoding="utf-8")
    for name in ("tests", "emptytests"):
        (root / "a/b" / name).mkdir(parents=True)
    env = LocalEnvironment(cwd=str(root))
    ops = ShellFileOperations(env, cwd=str(root))
    resolve = ops._resolve_command
    monkeypatch.setattr(ops, "_resolve_command", lambda cmd: rg if cmd == "rg" else resolve(cmd))
    pattern, path = {
        "files": ("*.py", str(root)),
        "directories": ("*tests", "a/b"),
        "invalid-glob": ("*}", str(root)),
    }[first]
    initial = ops.search(pattern, path, target="files")
    # Do not repair env.cwd between calls: both the supplemental find within one
    # search and every later relative operation use the actual observed cwd.
    if first == "invalid-glob":
        assert initial.error and "error parsing glob" in initial.error
    else:
        assert initial.error is None, initial.to_dict()
        expected = ({root / "sub/main.py"} if first == "files"
                    else {root / "a/b/tests", root / "a/b/emptytests"})
        assert {Path(root, name).resolve() for name in initial.files} == expected
    assert env.cwd == str(root)
    for _ in range(2):
        result = ops.search("*.py", "sub", target="files")
        assert result.error is None, result.to_dict()
        assert {Path(root, name).resolve() for name in result.files} == {root / "sub/main.py"}
        directories = ops.search("*tests", "a/b", target="files")
        assert directories.error is None, directories.to_dict()
        assert {Path(root, name).resolve() for name in directories.files} == {
            root / "a/b/tests", root / "a/b/emptytests"}
        content = ops.search("needle", "sub", target="content")
        assert content.error is None and content.matches
        read = ops.read_file("sub/main.py")
        assert read.error is None and "needle" in read.content
        missing = ops.read_file("sub/missing.py")  # includes same-directory ls suggestions
        assert missing.error
        assert env.cwd == str(root)


@pytest.mark.parametrize("cwd", ["~", "~/project", "relative/project", "/workspace/./project/.."])
@pytest.mark.parametrize("roots", [["sub"], ["/external", "sub"]])
def test_filename_coordinates_never_scope_to_an_unresolved_cwd(cwd, roots):
    scoped, _, operands = _filename_search_coordinates("*.py", roots, cwd)
    # An unresolved backend cwd must stay in its original coordinate system;
    # only an absolute backend cwd may be used for commonpath or a quoted cd.
    if not cwd.startswith("/"):
        assert scoped is None
        assert operands == roots
    else:
        assert scoped and os.path.isabs(scoped)
        assert all(os.path.isabs(os.path.join(scoped, operand)) for operand in operands)


@pytest.mark.parametrize("native", ["0", "1"], ids=["shell", "native"])
@pytest.mark.parametrize("pattern", ["*.py", "./main.py"])
def test_backend_tilde_cwd_and_mixed_roots_are_resolved_before_scoping(tmp_path, monkeypatch, native, pattern):
    rg = shutil.which("rg")
    if not rg:
        pytest.skip("requires ripgrep")
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
    root = tmp_path.resolve()
    for directory in ("sub", "other"):
        (root / directory).mkdir()
        (root / directory / "main.py").write_text("needle\n", encoding="utf-8")
    env = LocalEnvironment(cwd=str(root))
    ops = ShellFileOperations(env, cwd=str(root))
    home = ops._exec('printf %s "$HOME"').stdout
    # Bypass public search's path probes, as an SSH backend can reach the
    # coordinate helper before its first physical cwd marker has arrived.
    env.cwd = "~/" + os.path.relpath(root, home)
    result = ops._search_files_rg(pattern, ["sub", str(root / "other")],
                                  limit=50, offset=0, rg_executable=rg)
    assert result.error is None, result.to_dict()
    assert {Path(name).resolve() for name in result.files} == {
        root / "sub/main.py", root / "other/main.py"}
    assert env.cwd == str(root)
    repeat = ops._search_files_rg(pattern, ["sub", str(root / "other")],
                                  limit=50, offset=0, rg_executable=rg)
    assert repeat.error is None and set(repeat.files) == set(result.files)
    assert env.cwd == str(root)
