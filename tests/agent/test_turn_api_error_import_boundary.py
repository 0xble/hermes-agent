"""Error recovery must be importable without initializing tool backends."""

import subprocess
import sys


def test_error_recovery_import_does_not_initialize_terminal_backends():
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
import agent.turn_api_error
assert 'tools.terminal_tool_lifecycle' not in sys.modules
assert 'tools.environments.singularity' not in sys.modules
"""],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
