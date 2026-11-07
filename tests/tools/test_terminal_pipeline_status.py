"""The bash wrapper reports each pipeline segment's own exit status."""

import shlex
import shutil
import subprocess

import pytest

from tools.environments.base_session_env import _split_cwd_marker, _wrap_command_script

CWD_MARKER = "__HERMES_CWD_test__"
PIPE_MARKER = "__HERMES_PIPE_test__"


@pytest.fixture(params=sorted({path for name in ("bash", "/bin/bash") if (path := shutil.which(name))}))
def bash(request):
    return request.param


def _script(command: str, pipe_marker: str = PIPE_MARKER) -> str:
    return _wrap_command_script(
        command, quoted_cwd="/tmp", quoted_snap="/nonexistent-snapshot",
        snap_tmp_template="/tmp/hermes-snap.XXXXXX", passthrough_names=(),
        snapshot_ready=False, cwd_marker=CWD_MARKER, pipe_marker=pipe_marker,
    )


def _run_result(command: str, bash: str, pipe_marker: str = PIPE_MARKER) -> subprocess.CompletedProcess:
    return subprocess.run(
        [bash, "-c", _script(command, pipe_marker)], capture_output=True, text=True,
        timeout=10, check=False,
    )


def _run(command: str, bash: str) -> str:
    return _run_result(command, bash).stdout


def test_pipeline_status_captures_the_leading_command(bash):
    output = _run("false | true", bash)

    assert f"{PIPE_MARKER}1 0{PIPE_MARKER}" in output


def test_plain_command_reports_one_status(bash):
    output = _run("false", bash)

    assert f"{PIPE_MARKER}1{PIPE_MARKER}" in output


def _seen(result: subprocess.CompletedProcess) -> tuple[int, str, str]:
    """Exit code and output as the model sees them, markers stripped like production does."""
    stdout = result.stdout
    for marker in (PIPE_MARKER, CWD_MARKER):
        split = _split_cwd_marker(stdout, marker)
        stdout = split[1] if split else stdout
    return result.returncode, stdout, result.stderr


@pytest.mark.parametrize(
    ("command", "exit_code", "stdout"),
    [
        ("if false; then echo yes; fi", 0, ""),
        ("while read line; do echo \"$line\"; done < /dev/null", 0, ""),
        ("set -o pipefail; false | true", 1, ""),
        ("! false", 0, ""),
        ("! true", 1, ""),
        ("echo 'unbalanced", None, None),
        ('echo "unbalanced', None, None),
        ("echo $(echo unclosed", None, None),
        ("echo hi \\", None, None),
        ("cat <<'EOF'\nunterminated heredoc", 0, "unterminated heredoc\n"),
        ("exit 3", 3, ""),
        ("trap 'echo T' ERR; false", 1, "T\n"),
    ],
)
def test_capturing_pipestatus_changes_nothing_the_model_sees(command, exit_code, stdout, bash):
    """The capture is side data: the shell's own ``$?`` and output, no wrapper text."""
    seen_code, seen_stdout, seen_stderr = _seen(_run_result(command, bash))
    baseline_code, baseline_stdout, _baseline_stderr = _seen(_run_result(command, bash, pipe_marker=""))

    assert (seen_code, seen_stdout) == (baseline_code, baseline_stdout)
    if exit_code is None:
        # Bash 3.2 and modern Bash differ on eval parse errors and a trailing backslash.
        # Both wrapper modes must preserve this exact executable's native behavior.
        native = subprocess.run(
            [bash, "-c", f"eval {shlex.quote(command)}"], capture_output=True, text=True,
            timeout=10, check=False,
        )
        exit_code, stdout = native.returncode, native.stdout
    assert (seen_code, seen_stdout) == (exit_code, stdout)
    assert "__hermes" not in seen_stderr
