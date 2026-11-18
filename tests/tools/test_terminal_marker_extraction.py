"""Real-shell marker output must survive the production environment parser."""

import os
import shlex
import subprocess

import pytest

from tools.environments.base import BaseEnvironment
from tools.environments.base_session_env import _split_cwd_marker


pytestmark = pytest.mark.platforms("linux", "macos")


class _MarkerEnvironment(BaseEnvironment):
    def cleanup(self):
        pass


def _run(env, command):
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", env._wrap_command(command, env.cwd)],
        cwd="/", env={"PATH": os.defpath},
        capture_output=True, text=True, timeout=10, check=False,
    )
    result = {"output": completed.stdout, "returncode": completed.returncode}
    env._extract_cwd_from_output(result)
    return completed, result


@pytest.mark.parametrize(
    "command,statuses", [("false | true", [1, 0]), ("true | false", [0, 1]), ("false", [1])],
)
def test_real_wrapper_pipeline_status_reaches_environment_result(tmp_path, command, statuses):
    env = _MarkerEnvironment(cwd=str(tmp_path), timeout=10)
    completed, result = _run(env, command)

    assert result["pipeline_status"] == statuses
    assert result["returncode"] == statuses[-1]
    assert completed.stderr == ""
    assert result["output"] == ""
    assert result["cwd_observed"] is True
    assert result["cwd"] == str(tmp_path.resolve())
    assert env.cwd == str(tmp_path.resolve())


@pytest.mark.parametrize("body", ["", "pwd: error retrieving current directory", "/valid\ndiagnostic"])
def test_invalid_cwd_body_is_removed_without_replacing_previous_cwd(tmp_path, body):
    env = _MarkerEnvironment(cwd=str(tmp_path), timeout=10)
    output = f"before\n{env._cwd_marker}{body}{env._cwd_marker}\nafter\n"
    assert _split_cwd_marker(output, env._cwd_marker) == (None, "beforeafter\n")
    result = {"output": output}

    env._extract_cwd_from_output(result)

    assert env.cwd == str(tmp_path)
    assert result == {"output": "beforeafter\n"}


def test_command_removing_its_own_cwd_keeps_previous_cwd(tmp_path):
    removed = tmp_path / "removed directory"
    removed.mkdir()
    env = _MarkerEnvironment(cwd=str(tmp_path), timeout=10)
    completed, result = _run(env, f"cd {shlex.quote(str(removed))} && rmdir {shlex.quote(str(removed))}")

    assert completed.returncode == 0, completed
    assert completed.stderr == ""
    assert not removed.exists()
    assert env.cwd == str(tmp_path)
    assert "cwd" not in result
    assert "cwd_observed" not in result
    assert result["output"] == ""
