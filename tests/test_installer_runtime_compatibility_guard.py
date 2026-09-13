"""Bootstrap installers must defer managed-runtime updates to ``hermes update``.

Once an install carries ``runtime-compatibility.json``, the bootstrap scripts are
not allowed to mutate or replace its source tree. The guarded updater owns that
transition, including recovery and compatibility checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def _make_guarded_install(root: Path, git_shape: str) -> Path:
    install_dir = root / "hermes-agent"
    install_dir.mkdir()
    (install_dir / "sentinel.txt").write_text("preserve me\n", encoding="utf-8")

    if git_shape == "directory":
        (install_dir / ".git").mkdir()
    elif git_shape == "file":
        (install_dir / ".git").write_text("gitdir: ../worktree-git\n", encoding="utf-8")
    elif git_shape == "none":
        pass
    elif git_shape == "unreadable-marker":
        # A marker that cannot be parsed/read still means the managed-runtime
        # boundary exists. Presence, not readable contents, must fail closed.
        (install_dir / "runtime-compatibility.json").mkdir()
        return install_dir
    else:  # pragma: no cover - fixture misuse
        raise AssertionError(git_shape)

    (install_dir / "runtime-compatibility.json").write_text("{}\n", encoding="utf-8")
    return install_dir


@pytest.mark.parametrize("git_shape", ["directory", "file", "none", "unreadable-marker"])
def test_install_sh_refuses_guarded_existing_install_before_source_mutation(
    tmp_path: Path, git_shape: str
) -> None:
    if shutil.which("bash") is None:
        pytest.skip("needs bash")

    install_dir = _make_guarded_install(tmp_path, git_shape)
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "repository",
            "--json",
            "--dir",
            str(install_dir),
            "--hermes-home",
            str(tmp_path / "hermes-home"),
            "--commit",
            "deadbeef",
            "--force-commit",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "runtime-compatibility.json" in output
    assert "hermes update" in output
    assert (install_dir / "sentinel.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert not list(tmp_path.glob("hermes-agent.broken-*"))


@pytest.mark.windows_only
@pytest.mark.parametrize("git_shape", ["file", "unreadable-marker"])
def test_install_ps1_refuses_guarded_existing_install_before_source_mutation(
    tmp_path: Path, git_shape: str
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("needs PowerShell")

    install_dir = _make_guarded_install(tmp_path, git_shape)
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    env["USERPROFILE"] = str(tmp_path / "user-profile")
    env["TEMP"] = str(tmp_path / "temp")
    env["TMP"] = str(tmp_path / "temp")
    Path(env["TEMP"]).mkdir()
    Path(env["USERPROFILE"]).mkdir()

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_PS1),
            "-Stage",
            "repository",
            "-Json",
            "-InstallDir",
            str(install_dir),
            "-HermesHome",
            str(tmp_path / "hermes-home"),
            "-Commit",
            "deadbeef",
            "-ForceCommit",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "runtime-compatibility.json" in output
    assert "hermes update" in output
    assert (install_dir / "sentinel.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert not list(tmp_path.glob("hermes-agent.broken-*"))
