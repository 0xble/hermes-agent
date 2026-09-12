from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from scripts.ci import report_actions_usage as MODULE


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "report_actions_usage.py"
FIXTURE = ROOT / "tests" / "ci" / "fixtures" / "actions-usage-2026-09-01-03.json"


def test_historical_replay_counts_risk_revisions_and_unmatched_runs() -> None:
    summary = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = MODULE.project(summary)
    assert summary["risk_replay"]["risk_pull_request_count"] == 15
    confirmations = summary["risk_replay"]["risk_full_revision_count"] + summary["risk_replay"]["unmatched_summary_runs"]
    assert result.baseline_cost_usd == 58.51
    assert result.proposed_cost_usd == 17.57
    assert result.reduction_percent == 69.97
    assert result.full_confirmations == confirmations == 29
    assert result.smoke_minutes_per_pr == 1
    assert result.history_minutes_per_pr == 1
    assert result.proposed_minutes == {"linux": 2435, "windows": 116, "macos": 29}


def test_explicit_confirmation_override_is_a_scenario_not_the_default() -> None:
    summary = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = MODULE.project(summary, full_confirmations=17)
    assert result.full_confirmations == 17
    assert result.reduction_percent == 80.8
    assert MODULE.project(summary).proposed_cost_usd > result.proposed_cost_usd


def test_cli_reports_unmet_default_target_without_lowering_it() -> None:
    command = [sys.executable, str(SCRIPT), str(FIXTURE)]
    default = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert default.returncode == 1
    assert '"full_confirmations": 29' in default.stdout
    assert "below 80.00%" in default.stdout
    lower = subprocess.run(command + ["--minimum-reduction", "69"], cwd=ROOT, capture_output=True, text=True)
    assert lower.returncode == 0


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
