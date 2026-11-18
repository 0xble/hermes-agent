"""Packaged source selection preserves real probe revisions and archive contents."""
import io
import re
import signal
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.hermes_cli.immutable_test_helpers import clone_packaged_runtime


def test_packaged_runtime_archives_declared_roots_modules_and_probes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    metadata = '[tool.setuptools.packages.find]\ninclude = ["app", "app.*", "ops", "ops.*"]\n'
    runtime = {
        "pyproject.toml": metadata, "uv.lock": "version = 1\n",
        "root_module.py": "VALUE = 1\n", "app/__init__.py": "",
        "app/nested/__init__.py": "", "app/data.txt": "runtime data",
        "ops/__init__.py": "",
    }
    for name, body in {**runtime, "tests/test_unused.py": "", "web/index.html": ""}.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    def git(root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args])

    def commit(root):
        git(root, "-c", "user.name=S2", "-c", "user.email=s2@example.test",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
            "commit", "-qm", "probe revision")
        return git(root, "rev-parse", "HEAD").decode().strip()

    git(repo, "add", ".")
    commit(repo)
    source = tmp_path / "source"
    clone_packaged_runtime(repo, source)
    assert set(git(source, "ls-files").decode().splitlines()) == set(runtime)
    revisions = []
    for name in ("B", "B-prime"):
        (source / "probe.py").write_text("import app, ops, root_module\n", encoding="utf-8")
        (source / "version.txt").write_text(name, encoding="utf-8")
        git(source, "add", "probe.py", "version.txt")
        revisions.append((name, commit(source)))
    assert revisions[0][1] != revisions[1][1]
    for name, sha in revisions:
        with tarfile.open(fileobj=io.BytesIO(git(source, "archive", sha))) as archive:
            assert {item.name for item in archive if item.isfile()} == set(runtime) | {"probe.py", "version.txt"}
            assert archive.extractfile("version.txt").read().decode() == name


@pytest.mark.platforms("posix")
def test_launchd_phase_markers_survive_killed_pytest_capture(tmp_path):
    from scripts.run_tests_parallel import _run_one_file_once

    # A disposable child exercises capture and SIGKILL without loading any
    # launchd fixture or issuing a service command.
    child = tmp_path / "test_marker_child.py"
    child.write_text(
        "import os, signal\n"
        "from tests.hermes_cli.immutable_test_helpers import launchd_phase\n"
        "def test_marker(request):\n"
        "    with launchd_phase(request, 'completed'):\n"
        "        pass\n"
        "    with launchd_phase(request, 'interrupted'):\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n", encoding="utf-8")
    _, status, output, _, _ = _run_one_file_once(
        child, ["-q", "--capture=fd"], Path(__file__).resolve().parents[2], 300)
    assert status == -signal.SIGKILL, output
    markers = re.findall(r"\[launchd-real\] (\w+) (begin|end) (\d+\.\d+)", output)
    assert [(name, edge) for name, edge, _ in markers] == [
        ("completed", "begin"), ("completed", "end"), ("interrupted", "begin")]
    times = [float(timestamp) for _, _, timestamp in markers]
    assert times == sorted(times)
