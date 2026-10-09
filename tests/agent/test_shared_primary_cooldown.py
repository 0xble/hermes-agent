"""Cross-process regression tests for the shared primary cooldown."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _run(home: Path, code: str) -> dict:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_two_processes_share_one_cooldown_and_notice_claim(tmp_path):
    route = ("custom:fixture", "http://127.0.0.1:8317/v1", "primary")
    arm_code = f"""
import json
from agent.shared_primary_cooldown import arm_cooldown
record = arm_cooldown({route!r}, reason='rate_limit', reset_at=__import__('time').time() + 7200)
print(json.dumps(record))
"""
    first = _run(tmp_path, arm_code)
    assert first["backoff_count"] == 1
    assert first["source"] == "provider_reset"

    second = _run(
        tmp_path,
        f"""
import json
from agent.shared_primary_cooldown import active_cooldown, claim_outage_notice
route = {route!r}
record = active_cooldown(route)
print(json.dumps({{'active': bool(record), 'claim': claim_outage_notice(route, record['outage_id']), 'second_claim': claim_outage_notice(route, record['outage_id'])}}))
""",
    )
    assert second == {"active": True, "claim": True, "second_claim": False}

    third = _run(
        tmp_path,
        f"""
import json
from agent.shared_primary_cooldown import active_cooldown, claim_outage_notice
route = {route!r}
record = active_cooldown(route)
print(json.dumps({{'active': bool(record), 'claim': claim_outage_notice(route, record['outage_id'])}}))
""",
    )
    assert third == {"active": True, "claim": False}


def test_no_reset_backoff_survives_processes(tmp_path):
    route = ("custom:fixture", "http://127.0.0.1:8317/v1", "primary")
    first = _run(
        tmp_path,
        f"""
import json
from agent.shared_primary_cooldown import arm_cooldown
print(json.dumps(arm_cooldown({route!r}, reason='rate_limit', backoff_count=0)))
""",
    )
    second = _run(
        tmp_path,
        f"""
import json
from agent.shared_primary_cooldown import arm_cooldown
print(json.dumps(arm_cooldown({route!r}, reason='rate_limit', backoff_count=1)))
""",
    )
    assert first["reset_at"] - first["recorded_at"] >= 59
    assert second["backoff_count"] == 2
    assert second["reset_at"] - second["recorded_at"] >= 119
