"""Shared helpers for disposable immutable-release tests."""
from __future__ import annotations

import json
import sysconfig
import venv
from pathlib import Path


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
