from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from scripts.ci import report_actions_usage as MODULE


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "report_actions_usage.py"
FIXTURE = ROOT / "tests" / "ci" / "fixtures" / "actions-usage-2026-09-01-03.json"


def test_historical_replay_exceeds_reduction_contract() -> None:
    summary = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = MODULE.project(summary, smoke_minutes=3, full_confirmations=1)
    assert result.baseline_cost_usd == 58.51
    assert result.proposed_cost_usd < 4
    assert result.reduction_percent > 94
    assert result.proposed_minutes == {"linux": 523, "windows": 4, "macos": 1}


def test_cli_fails_when_minimum_is_impossible() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(FIXTURE),
            "--minimum-reduction",
            "99.99",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 1
    assert "below 99.99%" in completed.stdout
