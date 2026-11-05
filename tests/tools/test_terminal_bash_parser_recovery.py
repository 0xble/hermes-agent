"""Real-bash regressions for parser recovery and the session wrapper epilogue."""

import os
import shlex
import shutil
import subprocess

import pytest

from tools.environments.base_session_env import _split_cwd_marker, _wrap_command_script


pytestmark = pytest.mark.platforms("linux", "macos")
# Short marker reliably exposes Bash 5.2.15's heap corruption; a longer
# allocation can mask it even though the malformed eval is identical.
MARKER = "__CWD__"


def _script(command, cwd, snapshot, *, snapshot_ready=False, persist_env=True):
    return _wrap_command_script(
        command,
        quoted_cwd=shlex.quote(str(cwd)),
        quoted_snap=shlex.quote(str(snapshot)),
        snap_tmp_template=shlex.quote(str(snapshot) + ".tmp.XXXXXXXXXX"),
        passthrough_names=(),
        snapshot_ready=snapshot_ready,
        persist_env=persist_env,
        cwd_marker=MARKER,
    )


def _run(script):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required")
    return subprocess.run(
        # Resolve the binary but keep argv[0] short: its allocation, like
        # the marker and cwd, can mask the affected Bash parser size class.
        ["bash", "--noprofile", "--norc", "-c", script], executable=bash,
        cwd="/", env={"PATH": os.defpath},
        capture_output=True, text=True, timeout=10, check=False,
    )


@pytest.mark.parametrize("snapshot_ready,persist_env", [(False, True), (True, True), (True, False)])
def test_unclosed_command_substitution_preserves_status_cwd_and_snapshot(tmp_path, snapshot_ready, persist_env):
    snapshot = tmp_path / "snapshot"
    snapshot.write_text("export RECOVERY_STATE=retained\n", encoding="utf-8")
    # A short physical cwd, like the short marker, keeps Bash 5.2.15's
    # allocation in the size class that exposes the parser heap corruption.
    result = _run(_script(
        "echo $(echo unclosed", "/", snapshot,
        snapshot_ready=snapshot_ready, persist_env=persist_env,
    ))
    # Bash 3.2 returns 1 for this eval syntax error; modern Bash returns 2.
    # Neither may crash (a direct signal or the shell's 128+signal status).
    assert result.returncode > 0, result
    assert result.returncode not in (133, 134), result
    version = _run("printf '%s' \"${BASH_VERSINFO[0]}\"")
    assert version.returncode == 0, version
    if int(version.stdout) >= 4:
        assert result.returncode == 2, result
    assert "unexpected EOF" in result.stderr
    assert "free():" not in result.stdout + result.stderr
    assert "malloc" not in (result.stdout + result.stderr).lower()
    assert "Aborted" not in result.stderr
    assert _split_cwd_marker(result.stdout, MARKER) == ("/", "")
    if snapshot_ready:
        replay = _run(f"source {shlex.quote(str(snapshot))}; printf '%s' \"$RECOVERY_STATE\"")
        assert replay.returncode == 0, replay
        assert replay.stdout == "retained"
    assert not list(tmp_path.glob("snapshot.tmp.*"))


@pytest.mark.parametrize(
    "body", ["", "relative/path", "pwd: error retrieving current directory", "/valid\nnot-a-path", "/valid\n\n", "/valid\rdiagnostic"],
)
def test_cwd_parser_rejects_non_path_marker_bodies(body):
    assert _split_cwd_marker(f"before\n{MARKER}{body}{MARKER}\nafter\n", MARKER) == (
        None, "beforeafter\n",
    )


@pytest.mark.parametrize("body", ["/physical directory ", "/physical directory \n"])
def test_cwd_parser_preserves_path_spaces_and_strips_one_pwd_newline(body):
    assert _split_cwd_marker(f"{MARKER}{body}{MARKER}\n", MARKER) == (
        "/physical directory ", "",
    )


def test_pwd_function_cannot_exit_or_change_the_wrapper_shell(tmp_path):
    result = _run(_script(
        "pwd() { cd /; printf '/custom-cwd'; exit 7; }; set -e; true",
        tmp_path, tmp_path / "snapshot",
    ))
    assert result.returncode == 0, result
    assert result.stderr == ""
    assert _split_cwd_marker(result.stdout, MARKER) == ("/custom-cwd", "")


@pytest.mark.parametrize("persist_env", [True, False])
def test_valid_shell_syntax_and_physical_cwd_are_preserved(tmp_path, persist_env):
    real = tmp_path / "real directory"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    snapshot = tmp_path / "snapshot"
    snapshot.write_text(
        "shopt -s expand_aliases\nalias earlier_alias='earlier_function'\n"
        "earlier_function() { printf 'earlier:%s\\n' \"$1\"; }\n",
        encoding="utf-8",
    )
    command = (
        "cd link\n"
        "earlier_alias function\n"
        "shopt -s extglob\n"
        "[[ value == @(value|other) ]] || exit 99\n"
        "cat <<'EOF'\nhere-doc $(not expanded)\nEOF\n"
        "printf '%s\\n' \"$(printf 'valid substitution')\"\n"
        "export AFTER_COMMAND=retained\n"
    )
    result = _run(_script(command, tmp_path, snapshot, snapshot_ready=True, persist_env=persist_env))
    assert result.returncode == 0, result
    assert result.stderr == ""
    assert _split_cwd_marker(result.stdout, MARKER) == (
        str(real.resolve()), "earlier:function\nhere-doc $(not expanded)\nvalid substitution\n",
    )
    replay = _run(
        f"source {shlex.quote(str(snapshot))}\nearlier_alias replay\n"
        "printf '%s' \"${AFTER_COMMAND:-not-persisted}\""
    )
    assert replay.returncode == 0, replay
    assert replay.stdout == "earlier:replay\n" + ("retained" if persist_env else "not-persisted")
    assert not list(tmp_path.glob("snapshot.tmp.*"))