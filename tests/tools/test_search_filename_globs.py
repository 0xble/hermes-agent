"""Filename globs retain brace/path syntax and engine failures stay out of paths."""

import fnmatch
import os
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
        file.write_text("fixture\n", encoding="utf-8")

    def run(pattern, **kwargs):
        return ops.search(pattern, str(tmp_path), target="files", **kwargs)

    return run, tmp_path, ops


@pytest.mark.parametrize("pattern,expected", [
    (
        "{lefthook.yml,lefthook.yaml,.lefthook.yml,.lefthook.yaml,.githooks/*}",
        # Slash-bearing globs are passed verbatim: rg anchors them to the matched
        # path, so sub/lefthook.yaml is not a hit. Explicit globs select dotfiles.
        {"lefthook.yml", ".lefthook.yml"},
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
        (root / ".githooks/pre-commit").write_text("hook\n", encoding="utf-8")
    result = run(pattern)
    assert result.error is None, result.to_dict()
    assert {Path(file).relative_to(root).as_posix() for file in result.files} == expected


@pytest.mark.parametrize("pattern", ["**/tests", "**/emptydir", "**/**/emptydir"])
def test_recursive_basename_globs_include_nested_and_empty_directories(search, pattern):
    run, root, _ = search
    basename = pattern.rsplit("/", 1)[-1]
    expected = {basename, f"nested/{basename}"}
    for name in expected:
        (root / name).mkdir(parents=True)
    (root / basename / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    result = run(pattern)
    assert result.error is None, result.to_dict()
    assert {Path(file).relative_to(root).as_posix() for file in result.files} == expected


@pytest.mark.parametrize("pattern,expected", [
    ("./hooks/*", {"hooks/pre-commit"}),
    ("/hooks/*", {"hooks/pre-commit"}),
    ("{root}/hooks/*", {"hooks/pre-commit"}),
    ("./main.py", {"main.py"}),
    ("/main.py", {"main.py"}),
    ("{root}/main.py", {"main.py"}),
])
def test_filename_globs_normalize_relative_and_root_anchored_prefixes(search, pattern, expected):
    run, root, _ = search
    (root / "nested/hooks").mkdir(parents=True)
    (root / "nested/hooks/decoy").write_text("fixture\n", encoding="utf-8")
    (root / "main.py").write_text("fixture\n", encoding="utf-8")
    result = run(pattern.format(root=root))
    assert result.error is None, result.to_dict()
    assert {Path(file).relative_to(root).as_posix() for file in result.files} == expected


@pytest.mark.parametrize("root_form", ["relative", "absolute", "outside-cwd"])
@pytest.mark.parametrize("order", ["discovery", "modified"])
@pytest.mark.parametrize("pattern,expected", [
    ("/main.py", {"a/main.py", "b/main.py"}),
    ("/hooks/*.py", {"a/hooks/hook.py", "b/hooks/hook.py"}),
    ("{root}/a/main.py", {"a/main.py"}),
    ("{root}/b/hooks/*.py", {"b/hooks/hook.py"}),
])
def test_multiroot_anchored_globs_keep_each_root_and_absolute_identity(
    search, root_form, order, pattern, expected, monkeypatch,
):
    _, root, ops = search
    native_calls = []
    run_native = ops._run_rg_native

    def record_native(*args, **kwargs):
        native_calls.append(kwargs.get("cwd"))
        return run_native(*args, **kwargs)

    monkeypatch.setattr(ops, "_run_rg_native", record_native)
    native_enabled = ops._native_read_enabled()
    for dirname in ("a", "b"):
        for name in ("main.py", "nested/main.py", "hooks/hook.py", "nested/hooks/decoy.py"):
            file = root / dirname / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture\n", encoding="utf-8")
    roots = [root / "a", root / "b"]
    if root_form == "relative":
        roots = [file.relative_to(root) for file in roots]
    elif root_form == "outside-cwd":
        cwd = root / "caller-cwd"
        cwd.mkdir()
        ops.env.cwd = str(cwd)
        ops.cwd = str(cwd)
    result = ops.search(
        pattern.format(root=root), " ".join(str(file) for file in roots),
        target="files", order=order,
    )
    assert result.error is None, result.to_dict()
    actual = {Path(ops.env.cwd, file).resolve().relative_to(root.resolve()).as_posix() for file in result.files}
    assert actual == expected
    assert native_calls == ([str(root)] if native_enabled else [])


def test_multiroot_anchored_globs_escape_literal_root_names(search):
    _, root, ops = search
    names = ("a[1]", "b{2}")
    for name in names:
        (root / name).mkdir()
        (root / name / "main.py").write_text("fixture\n", encoding="utf-8")
    result = ops.search("/main.py", " ".join(names), target="files")
    assert result.error is None, result.to_dict()
    assert {Path(file).relative_to(root).as_posix() for file in result.files} == {
        f"{name}/main.py" for name in names
    }


@pytest.mark.parametrize("order", ["discovery", "modified"])
def test_filename_failures_report_bounded_diagnostics_separate_from_paths(search, order, monkeypatch):
    run, _, ops = search
    executions = []
    execute = ops.env.execute

    def record_execute(*args, **kwargs):
        result = execute(*args, **kwargs)
        executions.append(result)
        return result

    monkeypatch.setattr(ops.env, "execute", record_execute)
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
    if not ops._native_read_enabled():
        assert executions[-1]["cwd_observed"] is True


def _oracle_glob(parts, pattern):
    """Independent segment matcher: '*' never crosses '/', '**' may cross it."""
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return (_oracle_glob(parts, pattern[1:])
                or bool(parts) and _oracle_glob(parts[1:], pattern))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and _oracle_glob(parts[1:], pattern[1:])


def _walk_oracle(pattern, roots, cwd=None, operands=None):
    # Match the table in maintenance/bounded-filename-search.md, not production coordinates.
    cwd = Path(cwd or roots[0])
    operands = operands or [str(root) for root in roots]
    absolute_owner = next((root for root in sorted(roots, key=lambda p: len(str(p)), reverse=True)
                           if pattern.startswith(str(root) + "/")), None)
    anchored = pattern.startswith(("/", "./"))
    suffix = pattern[len(str(absolute_owner)) + 1:] if absolute_owner else pattern
    while suffix.startswith("./"):
        suffix = suffix[2:]
    suffix = suffix.lstrip("/")
    alternatives = [suffix]
    if "{" in suffix:
        before, choices = suffix.split("{", 1)
        choices, after = choices.split("}", 1)
        alternatives = [before + choice + after for choice in choices.split(",")]
    expected = set()
    for root, operand in zip(roots, operands):
        if absolute_owner and root != absolute_owner:
            continue
        for directory, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in files:
                candidate = Path(directory, name)
                relative = candidate.relative_to(root).as_posix()
                matched_path = relative
                if not anchored:
                    matched_path = (Path(operand) / candidate.relative_to(root)).as_posix()
                    if Path(operand).is_absolute() and candidate.is_relative_to(cwd):
                        matched_path = candidate.relative_to(cwd).as_posix()
                for alternative in alternatives:
                    if not anchored and "/" not in suffix and "{" not in suffix:
                        matches = fnmatch.fnmatchcase(name, "*" + alternative)
                    else:
                        glob = alternative
                        matches = _oracle_glob(matched_path.split("/"), glob.split("/"))
                    if matches:
                        expected.add(candidate.resolve())
    return expected


@pytest.mark.parametrize("root_count", [1, 2])
@pytest.mark.parametrize("location", ["cwd", "subdir", "outside"])
@pytest.mark.parametrize("spelling", ["relative", "absolute"])
@pytest.mark.parametrize("pattern_kind", [
    "root-file", "root-subdir", "absolute-inside", "absolute-outside",
    "basename", "recursive-dir", "brace", "dot-root", "path",
    "repeat-full-root", "repeat-root-basename",
])
def test_filename_coordinate_matrix(search, root_count, location, spelling, pattern_kind, monkeypatch):
    _, base, ops = search
    cwd = base / "caller"
    cwd.mkdir()
    first = {"cwd": cwd, "subdir": cwd / "sub/deep", "outside": base / "external/deep"}[location]
    roots = [first]
    if root_count == 2:
        roots.append(base / "unrelated/peer")
    for root in roots:
        for name in ("x", "prefix-x", "y", "sub/x", "sub/y", "sub/deep/x", "dir/x", "nested/dir/x", "nested/x"):
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture\n", encoding="utf-8")
    outside = base / "unselected/x"
    outside.parent.mkdir()
    outside.write_text("decoy\n", encoding="utf-8")
    patterns = {
        "root-file": "/x", "root-subdir": "/sub/*", "absolute-inside": str(first / "x"),
        "absolute-outside": str(outside), "basename": "x", "recursive-dir": "**/dir/x",
        "brace": "{x,sub/y}", "dot-root": "./x", "path": "sub/*",
        "repeat-full-root": os.path.relpath(first, cwd) + "/x",
        "repeat-root-basename": first.name + "/x",
    }
    pattern = patterns[pattern_kind]
    ops.cwd = ops.env.cwd = str(cwd)
    operands = [os.path.relpath(root, cwd) if spelling == "relative" else str(root) for root in roots]
    native_calls = []
    native = ops._run_rg_native

    def record_native(*args, **kwargs):
        native_calls.append(kwargs.get("cwd"))
        result = native(*args, **kwargs)
        assert result is not None, "matrix must exercise native rg without shell fallback"
        return result

    monkeypatch.setattr(ops, "_run_rg_native", record_native)
    result = ops.search(pattern, ", ".join(operands), target="files", limit=200)
    assert result.error is None, result.to_dict()
    actual = {Path(cwd, file).resolve() for file in result.files}
    assert actual == _walk_oracle(pattern, roots, cwd, operands)
    assert bool(native_calls) == ops._native_read_enabled()
    assert len(native_calls) <= 1
    if root_count == 2:
        single = ops.search(pattern, operands[0], target="files", limit=200)
        assert single.error is None, single.to_dict()
        under_first = {file for file in actual if file.is_relative_to(first.resolve())}
        assert under_first == {Path(cwd, file).resolve() for file in single.files}


@pytest.mark.parametrize("case", range(10))
def test_single_root_review_reproductions(search, case):
    # Verbatim review repros, cwd=D with D/a/main.py and D/a/sub/main.py:
    # search('/main.py','a'), search('/main.py','D/a'), search('/sub/*','a'),
    # search('D/a/main.py','a'), search('D/a/main.py','D/a'), search('/main.py','D').
    _, D, ops = search
    for name in ("main.py", "a/main.py", "a/sub/main.py"):
        file = D / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture\n", encoding="utf-8")
    pattern, root = [
        ("/main.py", "a"), ("/main.py", str(D / "a")), ("/sub/*", "a"),
        (str(D / "a/main.py"), "a"), (str(D / "a/main.py"), str(D / "a")),
        ("/main.py", str(D)),
        ("a/sub/*", "a/sub"), ("a/sub/*", f"a/sub, {D / 'hooks'}"),
        ("sub/*", "a/sub"), ("sub/*", f"a/sub, {D / 'hooks'}"),
    ][case]
    result = ops.search(pattern, root, target="files")
    assert result.error is None, result.to_dict()
    assert {Path(D, file).resolve() for file in result.files} == _walk_oracle(
        pattern, [Path(D, item) for item in root.split(", ")], D, root.split(", "))


def test_non_posix_anchoring_never_changes_shell_directory(search, monkeypatch):
    _, _, ops = search
    calls = []
    def forbid_execution(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError(f"non-POSIX anchoring must fail before execution: {kwargs}")

    monkeypatch.setattr(ops, "_run_rg_bounded", forbid_execution)
    result = ops._search_files_rg("/x", ["C:/project", "D:/other"], 50, 0)
    assert result.error and "POSIX" in result.error
    assert calls == []


@pytest.mark.parametrize("absolute_pattern", [False, True])
def test_filename_file_operand_never_becomes_cwd(search, absolute_pattern):
    _, root, ops = search
    selected = root / "main.py"
    selected.write_text("fixture\n", encoding="utf-8")
    pattern = str(selected) if absolute_pattern else "main.py"
    result = ops.search(pattern, str(selected), target="files")
    assert result.error is None, result.to_dict()
    assert {Path(root, file).resolve() for file in result.files} == {selected.resolve()}
