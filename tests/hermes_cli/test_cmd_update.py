"""Git trampoline recovery; branch updates use the real target-identity suite."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


@pytest.fixture(autouse=True)
def _isolate_venv_holders(monkeypatch):
    """The update flow's venv-holder guard sees the live gateway processes on
    a dev machine and aborts with SystemExit 2 before reaching the branch
    logic under test.  Isolate it so the test exercises the intended path."""
    monkeypatch.setattr("hermes_cli.update_cmd_windows._detect_venv_python_processes", list)


class TestGitTrampolineSelfHeal:
    """Proactive Git-for-Windows trampoline self-heal (#87876).

    A broken bin\\git.exe / cmd\\git.exe shim (~46KB) refuses every git call
    with a "BUG (fork bomb)" guard instead of re-execing the real git-core
    binary. _ensure_non_trampoline_git detects this up front and swaps in a
    real git binary when one can be located, so the normal git update path
    survives instead of degrading to the ZIP fallback.
    """

    @staticmethod
    def _fake_run_healthy(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout="git version 2.50.0.windows.1\n", stderr=""
        )

    @staticmethod
    def _fake_run_trampoline(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="BUG (fork bomb): tried to spawn itself, check your PATH\n",
        )

    @pytest.mark.platforms("windows")
    def test_healthy_git_command_unchanged(self):
        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_healthy,
            ),
            patch("hermes_cli.update_cmd._locate_real_git") as locate,
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        locate.assert_not_called()

    @pytest.mark.platforms("windows")
    def test_trampoline_swaps_to_real_git(self, capsys):
        from pathlib import Path

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        real = Path(r"C:\Program Files\Git\mingw64\libexec\git-core\git.exe")
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_trampoline,
            ),
            patch(
                "hermes_cli.update_cmd._locate_real_git", return_value=real
            ),
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == [str(real), "-c", "windows.appendAtomically=false"]
        out = capsys.readouterr().out
        assert "switching to real git" in out

    @pytest.mark.platforms("windows")
    def test_trampoline_no_real_git_keeps_command(self, capsys):
        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_trampoline,
            ),
            patch("hermes_cli.update_cmd._locate_real_git", return_value=None),
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        out = capsys.readouterr().out
        assert "ZIP path" in out

    @pytest.mark.platforms("not windows")
    def test_off_windows_noop(self):
        git_cmd = ["git"]
        with patch("hermes_cli.update_cmd.subprocess.run") as run:
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        run.assert_not_called()

    def test_portable_git_candidates_check_shared_root_first(self, tmp_path, monkeypatch):
        # Profile-scoped layout: HERMES_HOME = <root>/profiles/foo, but the
        # PortableGit tree lives under the SHARED root (monerostar review on
        # #88136). The candidate list must check get_default_hermes_root()
        # before the profile home.
        import hermes_constants
        from hermes_cli.update_cmd_git import _portable_git_candidates

        root = tmp_path / "root"
        profile_home = root / "profiles" / "foo"

        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: profile_home)

        candidates = _portable_git_candidates()
        git_core = ("libexec", "git-core", "git.exe")
        envs = ("clangarm64", "ucrt64", "mingw64", "mingw32")
        assert candidates == [
            *(root / "git" / env / Path(*git_core) for env in envs),
            *(profile_home / "git" / env / Path(*git_core) for env in envs),
        ]


def _fake_git_for_windows(root, env, *, output="git version 2.56.0.windows.2"):
    """A Git for Windows tree whose only git-core binary lives under *env*; it prints *output*."""
    exe = root / env / "libexec" / "git-core" / "git.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


@pytest.mark.platforms("not windows")  # the fake git.exe is a POSIX shell script
class TestLocateRealGitLayouts:
    """_locate_real_git across Git for Windows layouts, probing real executables.

    Git for Windows v2.56.0 moved the x64 tree from ``mingw64`` to ``ucrt64``; the
    locator must find git-core in either layout (and arm64's ``clangarm64``).
    """

    @pytest.fixture
    def roots(self, tmp_path, monkeypatch):
        from hermes_cli import update_cmd_git

        program_files = tmp_path / "Program Files" / "Git"
        monkeypatch.setattr(update_cmd_git, "_GIT_FOR_WINDOWS_ROOTS", (program_files,))
        monkeypatch.setattr(update_cmd_git, "_portable_git_candidates", lambda: [])
        return program_files

    @pytest.mark.parametrize("env", ["ucrt64", "mingw64", "clangarm64"])
    def test_finds_git_core_in_each_layout(self, roots, env):
        exe = _fake_git_for_windows(roots, env)
        assert update_cmd._locate_real_git() == exe

    def test_skips_a_guarded_candidate(self, roots):
        _fake_git_for_windows(roots, "ucrt64", output="BUG (fork bomb): check your PATH")
        healthy = _fake_git_for_windows(roots, "mingw64")
        assert update_cmd._locate_real_git() == healthy

    def test_none_without_git_core(self, roots):
        roots.mkdir(parents=True)
        assert update_cmd._locate_real_git() is None

    def test_finds_managed_portable_git_ucrt64(self, tmp_path, monkeypatch):
        import hermes_constants
        from hermes_cli import update_cmd_git

        monkeypatch.setattr(update_cmd_git, "_GIT_FOR_WINDOWS_ROOTS", ())
        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        exe = _fake_git_for_windows(tmp_path / "git", "ucrt64")
        assert update_cmd._locate_real_git() == exe
