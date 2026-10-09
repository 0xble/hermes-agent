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
    routes = json.loads(_record_path(home).read_text(encoding="utf-8-sig"))["routes"]
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
        record = json.loads(_record_path(home).read_text(encoding="utf-8-sig"))
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


# ── Cached fallback agent vs. a longer cooldown armed by another process (review round 2, P1) ─
#
# A long-lived (gateway-cached) agent goes to the fallback with a short in-memory window. Another
# process then re-arms the SAME outage with a much longer provider reset. When the cached agent's
# own window passes, its next turn must follow the shared record: stay on the fallback, leave the
# long reset in place for everyone, and make no primary call.

_CACHED_AGENT_CHILD = r'''
import json, os, sys, time
from run_agent import AIAgent
url, go_file, ready_file = sys.argv[1:4]
notices = []
fallback = [{"provider": "custom", "model": "fallback-model", "base_url": url, "api_key": "fixture", "api_mode": "chat_completions"}]
agent = AIAgent(
    api_key="fixture", base_url=url, provider="custom", model="primary-model",
    api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True,
    enabled_toolsets=[], fallback_model=fallback, max_iterations=3,
    status_callback=lambda kind, message: notices.append(str(message)),
)
first = agent.run_conversation("hello")
open(ready_file, "w").close()
deadline = time.time() + 120
while not os.path.exists(go_file):
    if time.time() > deadline:
        raise SystemExit("timed out waiting for the re-arm")
    time.sleep(0.05)
# This agent's own short window has passed (the gateway keeps the object across turns).
agent._rate_limited_until = 0
second = agent.run_conversation("hello again")
agent.close()
print(json.dumps({
    "first": first.get("final_response"), "second": second.get("final_response"),
    "model_after": agent.model, "notices": notices,
}))
'''


def test_cached_fallback_agent_honors_longer_cooldown_armed_by_another_process(tmp_path):
    home = tmp_path / ".hermes"
    write_home_config(home)
    go_file, ready_file = tmp_path / "go", tmp_path / "ready"
    env = os.environ.copy()
    env.update({"HERMES_HOME": str(home), "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    with StubProvider() as stub:
        stub.retry_after = None  # agent A arms only the short 60 s shared backoff
        agent_a = subprocess.Popen(
            [sys.executable, "-c", _CACHED_AGENT_CHILD, stub.url, str(go_file), str(ready_file)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.time() + 150
            while not ready_file.exists():
                assert agent_a.poll() is None, agent_a.communicate()
                assert time.time() < deadline, "agent A never finished its first turn"
                time.sleep(0.05)
            short = next(iter(json.loads(_record_path(home).read_text(encoding="utf-8-sig"))["routes"].values()))
            assert short["reset_at"] - short["recorded_at"] < 120, short
            # Process B probes, gets a 429 with a 2 h provider reset and re-arms the same outage.
            rearmed = _run(home, f"""
import json, time
from agent.shared_primary_cooldown import arm_cooldown, route_from_record
print(json.dumps(arm_cooldown(route_from_record({short!r}), reason="rate_limit", reset_at=time.time() + 7200)))
""")
            assert rearmed["outage_id"] == short["outage_id"]
            primary_before = len(stub.primary_requests())
            go_file.touch()
            out, err = agent_a.communicate(timeout=150)
        finally:
            if agent_a.poll() is None:
                agent_a.kill()
        assert agent_a.returncode == 0, err
        result = json.loads(out.strip().splitlines()[-1])
        primary_after = len(stub.primary_requests())
    after = next(iter(json.loads(_record_path(home).read_text(encoding="utf-8-sig"))["routes"].values()))
    assert result["first"] == "OK from fallback-model"
    assert primary_after - primary_before == 0, "the cached agent called the cooled primary"
    assert result["second"] == "OK from fallback-model", result
    assert result["model_after"] == "fallback-model", result
    assert after["outage_id"] == short["outage_id"]
    assert after["reset_at"] - time.time() > 7000, f"the long shared cooldown was overwritten: {after}"
    assert not [n for n in result["notices"] if "restored" in n], result["notices"]


# ── Stale outages (review round 2, P2-1) ─────────────────────────────────────────────────────

from agent import shared_primary_cooldown as spc  # noqa: E402

_ROUTE = ("custom:fixture", "http://127.0.0.1:8317/v1", "primary")


def _age_record(route, *, reset_ago, window):
    """Rewrite the record as if it expired ``reset_ago`` seconds ago after a ``window`` cooldown."""
    with spc._locked_state() as (path, state):
        entry = state["routes"][spc.route_key(provider=route[0], base_url=route[1], model=route[2])]
        entry["reset_at"] = time.time() - reset_ago
        entry["recorded_at"] = entry["reset_at"] - window
        spc._write_state(path, state)


def test_stale_record_starts_a_new_outage_with_a_fresh_notice_and_backoff():
    first = spc.arm_cooldown(_ROUTE, reason="rate_limit", backoff_count=0)
    for _ in range(3):  # escalate the shared level to 4
        spc.arm_cooldown(_ROUTE, reason="rate_limit")
    assert spc.claim_outage_notice(_ROUTE, first["outage_id"], fallback=("fb", "openai"))
    _age_record(_ROUTE, reset_ago=3 * 86_400, window=480)  # days later
    later = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    assert later["outage_id"] != first["outage_id"]
    assert later["notice_claimed"] is False
    assert "notice_fallback" not in later
    assert later["backoff_count"] == 1
    assert 59 <= later["reset_at"] - later["recorded_at"] <= 61
    assert spc.claim_outage_notice(_ROUTE, later["outage_id"]) is True


def test_recently_expired_record_is_still_the_same_outage():
    first = spc.arm_cooldown(_ROUTE, reason="rate_limit", backoff_count=0)
    assert spc.claim_outage_notice(_ROUTE, first["outage_id"])
    _age_record(_ROUTE, reset_ago=spc._STALE_GRACE_FLOOR_SECONDS - 60, window=60)
    again = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    assert again["outage_id"] == first["outage_id"]
    assert again["notice_claimed"] is True
    assert again["backoff_count"] == 2


def test_grace_scales_with_a_long_window():
    spc.arm_cooldown(_ROUTE, reason="rate_limit", reset_at=time.time() + 7200)
    # 30 min past a 2 h window: beyond the 10 min floor but inside the window-sized grace.
    _age_record(_ROUTE, reset_ago=1800, window=7200)
    assert spc.get_cooldown(_ROUTE) is not None
    _age_record(_ROUTE, reset_ago=7300, window=7200)
    assert spc.get_cooldown(_ROUTE) is None


def test_readers_prune_stale_records():
    other = ("custom:fixture", "http://127.0.0.1:8317/v1", "other")
    spc.arm_cooldown(_ROUTE, reason="rate_limit", reset_at=time.time() + 7200)
    spc.arm_cooldown(other, reason="rate_limit")
    _age_record(other, reset_ago=86_400, window=60)
    assert [r["model"] for r in spc.list_cooldowns()] == ["primary"]
    routes = json.loads(spc._state_path().read_text(encoding="utf-8-sig"))["routes"]
    assert [entry["model"] for entry in routes.values()] == ["primary"]


# ── Turn-start adoption details (review round 2, P2-2 and P3) ────────────────────────────────

def _arm_for(agent, seconds=7200):
    from agent.shared_primary_cooldown import arm_cooldown, route_from_agent
    return arm_cooldown(route_from_agent(agent), reason="rate_limit", reset_at=time.time() + seconds)


def test_stranded_fallback_index_does_not_send_the_turn_to_a_cooled_primary():
    with patch("agent.agent_runtime_helpers._adopt_shared_primary_cooldown", return_value=False):
        agent = _agent_with_chain(_CHAIN)
    _arm_for(agent)
    agent._fallback_index = len(_CHAIN)  # stranded by an earlier failed activation
    clients = {"fallback-one": (_fallback_client("https://one.invalid/v1"), "fallback-one")}
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        side_effect=lambda _provider, model=None, **_kw: clients[model],
    ):
        assert agent._restore_primary_runtime() is False
    assert agent.model == "fallback-one"
    assert agent._fallback_activated is True


def test_adoption_skips_the_state_file_without_a_fallback_chain():
    with patch("agent.agent_runtime_helpers._adopt_shared_primary_cooldown", return_value=False):
        agent = _agent_with_chain([])
    with patch("agent.shared_primary_cooldown.get_cooldown", side_effect=AssertionError("locked")):
        from agent.agent_runtime_helpers import _adopt_shared_primary_cooldown
        assert _adopt_shared_primary_cooldown(agent) is False
        assert agent._restore_primary_runtime() is False
    assert agent.model == "primary-model"


def test_cleared_cooldown_reaches_a_cached_fallback_agent_at_its_next_turn():
    agent = _agent_with_chain(_CHAIN)
    _activate(agent, reset_at=time.time() + 7200)
    assert agent.model == "fallback-one"
    assert agent._restore_primary_runtime() is False  # still cooling for everyone
    assert spc.clear_cooldowns(all_routes=True)
    assert agent._restore_primary_runtime() is True  # next turn start re-reads the record
    assert agent.model == "primary-model"


def test_cached_fallback_agent_adopts_a_longer_shared_window():
    agent = _agent_with_chain(_CHAIN)
    _activate(agent)  # 60 s shared backoff
    _arm_for(agent)  # another process re-arms the same outage for 2 h
    agent._rate_limited_until = 0  # this agent's own window has passed
    assert agent._restore_primary_runtime() is False
    assert agent.model == "fallback-one"
    assert agent._rate_limited_until - time.monotonic() > 7000
    assert spc.active_cooldown(spc.route_from_agent(agent))["reset_at"] - time.time() > 7000


# ── Exact cooldown clearing (review round 2, P2-3) ───────────────────────────────────────────

def test_clear_cooldowns_matches_exactly():
    a = ("custom:claude-proxy", "http://127.0.0.1:8317/v1", "claude-opus-5-5")
    b = ("custom:codex-proxy", "http://127.0.0.1:8317/v1", "claude-opus-5-5")
    c = ("custom:claude-proxy", "http://127.0.0.1:8317/v1", "claude-opus-5")
    for route in (a, b, c):
        spc.arm_cooldown(route, reason="rate_limit", reset_at=time.time() + 7200)
    assert spc.clear_cooldowns("opus") == []  # no substring matches
    assert spc.clear_cooldowns("claude-proxy") == []
    removed = spc.clear_cooldowns("CUSTOM:codex-proxy/claude-opus-5-5")
    assert [(r["provider"], r["model"]) for r in removed] == [("custom:codex-proxy", "claude-opus-5-5")]
    assert [r["model"] for r in spc.clear_cooldowns("claude-opus-5")] == ["claude-opus-5"]
    assert len(spc.clear_cooldowns(all_routes=True)) == 1
    with pytest.raises(ValueError):
        spc.clear_cooldowns()
    with pytest.raises(ValueError):
        spc.clear_cooldowns("x", all_routes=True)
