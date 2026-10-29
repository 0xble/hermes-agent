"""A launch-time relaunch of an inline ``-c`` launcher replays its original argv.

Regression: the release interpreter re-enters the source checkout with
``python -c "...sys.path.insert(0, sys.argv.pop(1))..." <source> update --check``.
That code pops the source path, then ``hermes_bootstrap`` relaunched under the managed
interpreter with the already-popped ``sys.argv``. The replayed code popped again, ate
the subcommand, and argparse rejected ``--check`` / ``--plan`` as unrecognized arguments.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.venv_sync import relaunch_command

# Same shape as the release-to-source re-entry in ``hermes_cli.main.cmd_update``; it only
# echoes argv. The token stands in for the subcommand (the live-system guard rightly refuses
# to spawn anything spelled like a real ``hermes update``).
_REENTRY = "import json,sys;sys.path.insert(0,sys.argv.pop(1));print(json.dumps(sys.argv[1:]))"


@pytest.mark.parametrize("flag", ["--check", "--plan"])
def test_relaunched_inline_launcher_sees_its_arguments_once(tmp_path, flag):
    original = [sys.executable, "-I", "-c", _REENTRY, str(tmp_path), "subcommand", flag]
    first_run = subprocess.run(original, capture_output=True, text=True, check=True)
    assert json.loads(first_run.stdout) == ["subcommand", flag]

    # What bootstrap observes inside that first run, after the launcher popped its path.
    observed_argv = ["-c", "subcommand", flag]
    command = relaunch_command(Path(sys.executable), tmp_path, observed_argv, original, None)
    relaunched = subprocess.run(command, capture_output=True, text=True, check=True)

    assert json.loads(relaunched.stdout) == ["subcommand", flag]
