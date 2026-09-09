"""Saved restart history must not claim knowledge of live runtimes."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("pending", [False, True])
def test_real_cli_startup_preserves_saved_update_record(tmp_path, pending):
    home = tmp_path / "home"
    home.mkdir()
    marker = home / "fleet_restart_pending"
    record = "started=1\npid=99999999\nexpected_sha=" + "a" * 40 + "\n"
    if pending:
        marker.write_text(record, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(home), HERMES_TEST_ISOLATION=str(home))
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "--help"],
        cwd=Path(__file__).resolve().parents[2], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "did not restart running gateways" not in result.stderr
    if pending:
        assert "did not verify fleet restart completion" in result.stderr
        assert "Runtimes may already have been restarted separately" in result.stderr
        assert "hermes update --plan" in result.stderr
        assert marker.read_text(encoding="utf-8") == record
    else:
        assert "fleet restart completion" not in result.stderr
        assert not marker.exists()
