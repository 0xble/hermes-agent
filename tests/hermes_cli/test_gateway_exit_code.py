"""A gateway subcommand's int return code must become the ``hermes`` process exit status.

``gateway update`` and ``gateway guardian`` report failure by returning a non-zero int;
shell callers, cron and launchd only see it if ``cmd_gateway`` hands it to ``main()``.
"""
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("handler_result,expected_status", [(2, 2), (None, 0)])
def test_gateway_handler_result_is_process_exit_status(handler_result, expected_status):
    program = f"""
import sys
import hermes_cli.gateway as gateway
import hermes_cli.main as cli_main
cli_main._sync_bundled_skills_quietly = lambda: None
def handler(args):
    print('fixture gateway handler ran')
    return {handler_result!r}
gateway._GATEWAY_SUBCOMMANDS['status'] = handler
sys.argv = ['hermes', 'gateway', 'status']
cli_main.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert "fixture gateway handler ran" in result.stdout, result.stdout + result.stderr
    assert result.returncode == expected_status, result.stdout + result.stderr
