"""Shared helpers for disposable immutable-release tests."""
from __future__ import annotations

import json
import subprocess
import sys
import sysconfig
import time
import tomllib
import venv
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def launchd_phase(request, name):
    """Expose phase boundaries through pytest capture to the parent runner."""
    # fd capture keeps even flushed stderr in pytest's private temp file until
    # the test ends. Suspend it only for markers so the runner's pipe retains
    # the last phase when it SIGKILLs a worker at the unchanged file deadline.
    capture = request.config.pluginmanager.getplugin("capturemanager")

    def mark(edge):
        with capture.global_and_fixture_disabled():
            print(f"[launchd-real] {name} {edge} {time.monotonic():.6f}",
                  file=sys.stderr, flush=True)

    mark("begin")
    try:
        yield
    finally:
        mark("end")


def clone_packaged_runtime(repo: Path, source: Path) -> None:
    """Seed the source index with the declared runtime before probe revisions."""
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout",
                    str(repo), str(source)], check=True, timeout=60)
    metadata = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    packages = sorted({name.split(".")[0] for name in
                       metadata["tool"]["setuptools"]["packages"]["find"]["include"]})
    subprocess.run(["git", "-C", str(source), "read-tree", "--empty"], check=True)
    subprocess.run(["git", "-C", str(source), "restore", "--source=HEAD", "--staged", "--worktree", "--",
                    ":(top,glob)*.py", "pyproject.toml", "uv.lock", *packages], check=True)


def _fake_release_generation(release: Path, extras: list[str]) -> Path:
    """Commit an empty generation whose Python can execute real metadata probes."""
    from pm.environments import install_state_dir, runtime_facts_path

    environment = install_state_dir(release) / "environments" / "generation" / "venv"
    # An empty cfg + a symlink to sys.executable loses the base stdlib when
    # the runner's interpreter is a copied venv executable. Let venv record
    # the base interpreter instead; do not inherit the runner's packages.
    venv.EnvBuilder(with_pip=False).create(environment)
    runtime_facts_path(release).write_text(json.dumps({"schema": 1, "packages": {"venv": {
        "environment": str(environment), "extras": extras}}}), encoding="utf-8")
    return environment


def _build_test_venv(release: Path, **_kwargs) -> None:
    """Create a candidate interpreter while reusing the runner's test dependencies."""
    venv_dir = release / ".venv"
    venv.EnvBuilder(with_pip=False).create(venv_dir)
    site = next((venv_dir / "lib").glob("python*/site-packages"))
    site.joinpath("hermes_test_host_deps.pth").write_text(
        sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8")
    launcher = venv_dir / "bin" / "hermes"
    # This stub exists only for PATH/``which hermes`` identity resolution.
    launcher.write_text(f"#!{venv_dir / 'bin' / 'python'}\n", encoding="utf-8")
    launcher.chmod(0o755)
