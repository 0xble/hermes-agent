"""Cross-process regression tests for the shared primary cooldown."""
from __future__ import annotations

import json
import os
import subprocess
import time
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
    assert result["before"] == "fallback-model"
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



# ── Real request path (AIAgent.run_conversation against a loopback stub) ────────────────────
#
# These drive the production wrappers rather than calling the client or the recovery helper by
# hand: every successful response passes through the hook that may clear the shared outage, so
# a fallback reply must never be mistaken for primary recovery.

import pytest

from tests.agent._shared_cooldown_stub import StubProvider, run_turn, write_home_config

_REQUEST_PATHS = ("stream", "nonstream", "direct")


def _record_path(home: Path) -> Path:
    return home / "state" / "model_cooldowns.json"


@pytest.mark.parametrize("mode", _REQUEST_PATHS)
def test_fallback_reply_keeps_outage_and_emits_no_recovery(tmp_path, mode):
    home = tmp_path / ".hermes"
    write_home_config(home)
    with StubProvider() as stub:
        result = run_turn(home, stub.url, mode)
        primary = stub.primary_requests()
    assert result["final_response"] == "OK from fallback-model"
    assert [r["status"] for r in primary] == [429]
    assert _record_path(home).exists(), "a fallback reply must not clear the primary outage"
    routes = json.loads(_record_path(home).read_text(encoding="utf-8"))["routes"]
    (entry,) = routes.values()
    assert entry["model"] == "primary-model"
    assert entry["reset_at"] - entry["recorded_at"] > 7000
    assert not [n for n in result["notices"] if "restored" in n], result["notices"]
    assert len([n for n in result["notices"] if "rate-limited until" in n]) == 1, result["notices"]


@pytest.mark.parametrize("mode", _REQUEST_PATHS)
def test_primary_success_after_reset_clears_outage_with_one_recovery_notice(tmp_path, mode):
    home = tmp_path / ".hermes"
    write_home_config(home)
    with StubProvider() as stub:
        run_turn(home, stub.url, mode)  # arm the outage through the real path
        record = json.loads(_record_path(home).read_text(encoding="utf-8"))
        for entry in record["routes"].values():
            entry["reset_at"] = time.time() - 1
        _record_path(home).write_text(json.dumps(record), encoding="utf-8")
        stub.primary_available = True
        result = run_turn(home, stub.url, mode)
        primary = stub.primary_requests()
    assert result["final_response"] == "OK from primary-model"
    assert result["model"] == "primary-model"
    assert [r["status"] for r in primary] == [429, 200]
    assert not _record_path(home).exists(), "a real primary success must clear the outage"
    assert len([n for n in result["notices"] if "restored" in n]) == 1, result["notices"]


# ── Notice ownership (P2): never silently drop a rate-limit fallback notice ─────────────────

from unittest.mock import MagicMock, patch  # noqa: E402

from agent.error_classifier import FailoverReason  # noqa: E402


def _agent_with_chain(chain):
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="primary-key", base_url="https://primary.invalid/v1", provider="custom",
            model="primary-model", api_mode="chat_completions", quiet_mode=True,
            skip_context_files=True, skip_memory=True, fallback_model=chain,
        )
    agent.client = MagicMock()
    agent._pending_fallback_notice = None
    return agent


def _fallback_client(base_url):
    client = MagicMock()
    client.base_url = base_url
    client.api_key = "fallback-key"
    return client


_CHAIN = [
    {"provider": "openai", "model": "fallback-one", "base_url": "https://one.invalid/v1"},
    {"provider": "zai", "model": "fallback-two", "base_url": "https://two.invalid/v1"},
]


def _activate(agent, reason=FailoverReason.rate_limit, reset_at=None):
    clients = {
        "fallback-one": (_fallback_client("https://one.invalid/v1"), "fallback-one"),
        "fallback-two": (_fallback_client("https://two.invalid/v1"), "fallback-two"),
    }
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        side_effect=lambda _provider, model=None, **_kw: clients[model],
    ):
        assert agent._try_activate_fallback(reason, reset_at=reset_at) is True


def _pending(agent):
    return list(getattr(agent, "_pending_fallback_notice", None) or [])


@pytest.mark.parametrize("failure", ["raises", "returns_none"])
def test_rate_limit_notice_survives_shared_write_failure(failure):
    agent = _agent_with_chain(_CHAIN)

    def _broken(*_args, **_kwargs):
        if failure == "raises":
            raise OSError("read-only state dir")
        return None

    with patch("agent.shared_primary_cooldown.arm_cooldown", side_effect=_broken):
        _activate(agent, reset_at=time.time() + 7200)
    notices = _pending(agent)
    assert len(notices) == 1, notices
    assert "fallback-one" in notices[0] and "primary-model" in notices[0]


def test_switch_to_a_different_fallback_under_rate_limit_still_notifies():
    agent = _agent_with_chain(_CHAIN)
    _activate(agent, reset_at=time.time() + 7200)
    first = _pending(agent)
    assert len(first) == 1 and "rate-limited until" in first[0] and "fallback-one" in first[0]
    agent._pending_fallback_notice = None
    _activate(agent)  # fallback-one is now rate-limited too: move to fallback-two
    second = _pending(agent)
    assert len(second) == 1, second
    assert "fallback-two" in second[0]


def test_rearm_onto_the_announced_fallback_stays_silent():
    first = _agent_with_chain(_CHAIN)
    _activate(first, reset_at=time.time() + 7200)
    assert len(_pending(first)) == 1
    # A second agent in the same outage that itself hits the primary lands on the model the
    # claimed notice already announced: one notice per outage.
    # Construct it before adoption would move it, so it starts on the primary as a session that
    # was already mid-turn when the outage began.
    with patch("agent.agent_runtime_helpers._adopt_shared_primary_cooldown", return_value=False):
        second = _agent_with_chain(_CHAIN)
    assert second.model == "primary-model"
    _activate(second, reset_at=time.time() + 7200)
    assert second.model == "fallback-one"
    assert _pending(second) == []
