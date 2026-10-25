"""Shared helpers for disposable immutable-release tests."""
from __future__ import annotations

import sysconfig
import venv
from pathlib import Path


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
