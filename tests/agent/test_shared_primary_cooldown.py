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

def test_fresh_agent_after_eviction_adopts_cooldown_without_primary_call(tmp_path):
    """The gateway eviction regression: a second AIAgent reads the durable record at turn start."""
    result = _run(
        tmp_path,
        """
import json
from unittest.mock import MagicMock, patch
from run_agent import AIAgent
from agent.error_classifier import FailoverReason

fallback = [{"provider": "openai", "model": "fallback-model", "base_url": "https://fallback.invalid/v1"}]
with patch("model_tools.get_tool_definitions", return_value=[]), patch("model_tools.check_toolset_requirements", return_value={}), patch("agent.process_bootstrap.OpenAI"):
    with patch("agent.auxiliary_client.resolve_provider_client") as resolve:
        client = MagicMock()
        client.base_url = "https://fallback.invalid/v1"
        client.api_key = "fallback-key"
        resolve.return_value = (client, "fallback-model")
        first = AIAgent(api_key="primary-key", base_url="https://primary.invalid/v1", provider="custom", model="primary-model", api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True, fallback_model=fallback)
        first._try_activate_fallback(reason=FailoverReason.rate_limit)
        first.close()
        second = AIAgent(api_key="primary-key", base_url="https://primary.invalid/v1", provider="custom", model="primary-model", api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True, fallback_model=fallback)
        before = second.model
        second._restore_primary_runtime()
        after = second.model
        second.close()
print(json.dumps({"before": before, "after": after, "fallback_calls": resolve.call_count}))
""",
    )
    assert result["before"] == "primary-model"
    assert result["after"] == "fallback-model"
    assert result["fallback_calls"] >= 1


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

