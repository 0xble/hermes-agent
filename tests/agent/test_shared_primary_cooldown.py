"""Cross-process regression tests for the shared primary cooldown."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import UTC, datetime
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace


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
    # A real escalation follows a re-probe after the window lapsed. A 429 that was already in
    # flight inside the window must not escalate (test_in_flight_burst_does_not_escalate_backoff).
    state_path = tmp_path / "state" / "model_cooldowns.json"
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    for entry in state["routes"].values():
        entry["reset_at"] = time.time() - 1
    state_path.write_text(json.dumps(state), encoding="utf-8")
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
    state = json.loads(_record_path(home).read_text(encoding="utf-8-sig"))
    assert any(entry.get("state") == "tombstone" for entry in state["routes"].values())
    assert len([n for n in result["notices"] if "restored" in n]) == 1, result["notices"]


# ── Notice ownership (P2): never silently drop a rate-limit fallback notice ─────────────────

from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason


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

from agent import shared_primary_cooldown as spc

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


def test_in_flight_burst_does_not_escalate_backoff():
    """Concurrent in-flight 429s at outage start share one level, not 60 -> 960 s."""
    windows = []
    for _ in range(5):
        entry = spc.arm_cooldown(_ROUTE, reason="rate_limit", backoff_count=0)
        windows.append(round(entry["reset_at"] - entry["recorded_at"]))
    assert windows == [60, 60, 60, 60, 60]
    assert entry["backoff_count"] == 1


def test_no_reset_rearm_never_shortens_an_active_provider_reset():
    long = spc.arm_cooldown(_ROUTE, reason="rate_limit", reset_at=time.time() + 7200)
    again = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    assert again["reset_at"] >= long["reset_at"] - 1
    assert again["source"] == "provider_reset"
    assert again["outage_id"] == long["outage_id"]


def test_provider_reset_still_wins_over_an_active_window():
    spc.arm_cooldown(_ROUTE, reason="rate_limit", reset_at=time.time() + 7200)
    sooner = time.time() + 600
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit", reset_at=sooner)
    assert abs(entry["reset_at"] - sooner) < 1


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
    with patch("agent.shared_primary_cooldown.read_cooldown", side_effect=AssertionError("locked")):
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


def test_shortened_shared_window_releases_a_cached_fallback_agent_once_it_expires():
    """A provider reset that shortens the outage must not leave the old local deadline blocking."""
    agent = _agent_with_chain(_CHAIN)
    _activate(agent, reset_at=time.time() + 7200)  # 2 h outage, local deadline set from it
    assert agent._rate_limited_until - time.monotonic() > 7000
    _arm_for(agent, seconds=60)  # a later provider reset shortens the shared window to 60 s
    _age_record(spc.route_from_agent(agent), reset_ago=1, window=60)  # 61 s later: expired
    assert agent._restore_primary_runtime() is True
    assert agent.model == "primary-model"
    # The expired record stays for the recovery probe: a primary success clears it with one notice.
    assert spc.read_cooldown(spc.route_from_agent(agent)) is not None


def test_expired_shared_record_does_not_release_a_longer_local_window_without_a_record():
    """No shared record held (shared state unavailable or never read) keeps the local deadline."""
    agent = _agent_with_chain(_CHAIN)
    _activate(agent, reset_at=time.time() + 7200)
    with patch("agent.shared_primary_cooldown.read_cooldown", side_effect=OSError("unreadable")):
        assert agent._restore_primary_runtime() is False
    assert agent.model == "fallback-one"


# ── Exact cooldown clearing (review round 2, P2-3) ───────────────────────────────────────────

def test_clear_cooldowns_matches_exactly():
    a = ("custom:claude-proxy", "http://127.0.0.1:8317/v1", "claude-opus-5-5")
    b = ("custom:openai-proxy", "http://127.0.0.1:8317/v1", "claude-opus-5-5")
    c = ("custom:claude-proxy", "http://127.0.0.1:8317/v1", "claude-opus-5")
    for route in (a, b, c):
        spc.arm_cooldown(route, reason="rate_limit", reset_at=time.time() + 7200)
    assert spc.clear_cooldowns("opus") == []  # no substring matches
    assert spc.clear_cooldowns("claude-proxy") == []
    removed = spc.clear_cooldowns("CUSTOM:openai-proxy/claude-opus-5-5")
    assert [(r["provider"], r["model"]) for r in removed] == [("custom:openai-proxy", "claude-opus-5-5")]
    assert [r["model"] for r in spc.clear_cooldowns("claude-opus-5")] == ["claude-opus-5"]
    assert len(spc.clear_cooldowns(all_routes=True)) == 1
    with pytest.raises(ValueError):
        spc.clear_cooldowns()
    with pytest.raises(ValueError):
        spc.clear_cooldowns("x", all_routes=True)


def test_bedrock_stream_success_runs_primary_recovery(monkeypatch):
    """All four primary success paths (stream, non-stream, direct, Bedrock) clear the outage."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent import chat_completion_helpers as h

    recovered = []
    monkeypatch.setattr(spc, "complete_primary_recovery", lambda agent: recovered.append(agent) or True)
    agent = SimpleNamespace(_interrupt_requested=False, _consecutive_stale_streams=0)
    stream = h._BedrockStream.__new__(h._BedrockStream)
    stream.agent = agent
    stream.result = {"response": object(), "error": None}
    stream.last_event = time.time()
    stream.stale_timeout = 60
    stream._worker = lambda: None
    stream._raise_if_interrupted = MagicMock()
    stream._on_stale = MagicMock()

    assert stream._poll() is stream.result["response"]
    assert recovered == [agent]


# ── Overload shares the primary cooldown ────────────────────────────────────────────────────
#
# A 529, or a 503 whose body says overloaded, classifies as ``overloaded``. It arms the same
# shared record as a 429: fresh processes skip the primary, one outage notice per outage, and the
# no-reset backoff follows the rate-limit rules. Generic 500s and timeouts keep their old
# per-session behavior.

def _overload_turns(home, stub, mode="stream"):
    first = run_turn(home, stub.url, mode)
    first_primary = stub.primary_requests()
    second = run_turn(home, stub.url, mode)
    return first, second, first_primary, len(stub.primary_requests()) - len(first_primary)


@pytest.mark.parametrize(("status", "retry_after"), [(529, "7200"), (503, None)])
def test_overload_arms_the_shared_cooldown_across_processes(tmp_path, status, retry_after):
    home = tmp_path / ".hermes"
    write_home_config(home)
    with StubProvider() as stub:
        stub.failure_status = status
        stub.retry_after = retry_after
        first, second, first_primary, second_calls = _overload_turns(home, stub)
    assert first["final_response"] == second["final_response"] == "OK from fallback-model"
    # A streaming 5xx is re-issued once without streaming inside the same request (the existing
    # unmask probe), so the arming turn may hit the primary twice; both are the one outage.
    assert first_primary and {r["status"] for r in first_primary} == {status}
    assert second_calls == 0, "a fresh process called the overloaded primary"
    assert _record_path(home).exists(), "an overload did not arm the shared record"
    (entry,) = json.loads(_record_path(home).read_text(encoding="utf-8-sig"))["routes"].values()
    assert entry["reason"] == "overloaded"
    window = entry["reset_at"] - entry["recorded_at"]
    if retry_after:
        assert window > 7000, entry  # Retry-After on a 529 goes through the provider-reset path
    else:
        assert 59 <= window <= 61, entry
    notices = first["notices"] + second["notices"]
    assert len([n for n in notices if "is overloaded until" in n]) == 1, notices
    assert not [n for n in notices if "Model fallback:" in n], notices


def test_overload_no_reset_backoff_escalates_only_after_the_window_lapses():
    first = _agent_with_chain(_CHAIN)
    _activate(first, reason=FailoverReason.overloaded)
    route = spc.route_from_agent(first)
    armed = spc.active_cooldown(route)
    assert armed is not None and armed["reason"] == "overloaded"
    assert 59 <= armed["reset_at"] - armed["recorded_at"] <= 61
    # An overload that was already in flight lands inside the window: same level, same outage.
    with patch("agent.agent_runtime_helpers._adopt_shared_primary_cooldown", return_value=False):
        in_flight = _agent_with_chain(_CHAIN)
    _activate(in_flight, reason=FailoverReason.overloaded)
    held = spc.active_cooldown(route)
    assert held is not None
    assert held["outage_id"] == armed["outage_id"] and held["backoff_count"] == 1
    assert 59 <= held["reset_at"] - held["recorded_at"] <= 61
    assert _pending(in_flight) == []  # the claimed outage notice already covers it
    # The window lapsed and a real re-probe is overloaded again: 120 s.
    _age_record(route, reset_ago=1, window=60)
    with patch("agent.agent_runtime_helpers._adopt_shared_primary_cooldown", return_value=False):
        probe = _agent_with_chain(_CHAIN)
    _activate(probe, reason=FailoverReason.overloaded)
    later = spc.active_cooldown(route)
    assert later is not None
    assert later["outage_id"] == armed["outage_id"] and later["backoff_count"] == 2
    assert 119 <= later["reset_at"] - later["recorded_at"] <= 121


@pytest.mark.parametrize("reason", [FailoverReason.server_error, FailoverReason.timeout])
def test_generic_server_errors_and_timeouts_do_not_arm(reason):
    agent = _agent_with_chain(_CHAIN)
    _activate(agent, reason=reason)
    assert spc.list_cooldowns() == []
    assert not spc._state_path().exists()
    notices = _pending(agent)
    assert len(notices) == 1 and "Model fallback:" in notices[0], notices


def test_generic_500_on_the_real_path_does_not_arm(tmp_path):
    home = tmp_path / ".hermes"
    write_home_config(home)
    with StubProvider() as stub:
        stub.failure_status = 500
        stub.retry_after = None
        result = run_turn(home, stub.url)
    assert result["final_response"] == "OK from fallback-model"
    assert not _record_path(home).exists()


# ── Malformed records: non-finite or implausibly distant resets ──────────────────────────────────────────

def _write_routes(entries):
    routes = {
        spc.route_key(provider=e["provider"], base_url=e["base_url"], model=e["model"]): e for e in entries
    }
    path = spc._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "routes": routes}), encoding="utf-8")


def test_non_finite_records_are_pruned_by_readers():
    now = time.time()
    base = {"provider": "custom:fixture", "base_url": "http://127.0.0.1:8317/v1", "reason": "rate_limit",
            "outage_id": "x", "backoff_count": 1}
    _write_routes([
        {**base, "model": "reset-inf", "reset_at": float("inf"), "recorded_at": now},
        {**base, "model": "reset-nan", "reset_at": float("nan"), "recorded_at": now},
        {**base, "model": "reset-neg-inf", "reset_at": float("-inf"), "recorded_at": now},
        {**base, "model": "recorded-inf", "reset_at": now + 600, "recorded_at": float("inf")},
        {**base, "model": "healthy", "reset_at": now + 600, "recorded_at": now},
    ])
    assert [r["model"] for r in spc.list_cooldowns()] == ["healthy"]
    routes = json.loads(spc._state_path().read_text(encoding="utf-8-sig"))["routes"]
    assert [entry["model"] for entry in routes.values()] == ["healthy"]


def test_implausibly_distant_reset_is_pruned_but_a_weekly_cap_is_kept():
    now = time.time()
    base = {"provider": "custom:fixture", "base_url": "http://127.0.0.1:8317/v1", "reason": "rate_limit",
            "outage_id": "x", "backoff_count": 1, "recorded_at": now}
    _write_routes([
        {**base, "model": "huge", "reset_at": 1e20},
        {**base, "model": "past-ceiling", "reset_at": now + spc._MAX_PROVIDER_RESET_SECONDS + 3600},
        {**base, "model": "weekly-cap", "reset_at": now + 6 * 86_400},
    ])
    assert [r["model"] for r in spc.list_cooldowns()] == ["weekly-cap"]
    assert spc.active_cooldown(("custom:fixture", "http://127.0.0.1:8317/v1", "huge")) is None


def test_arm_keeps_a_multi_day_provider_reset():
    """The original motivating case: a usage cap that resets days away must be honored."""
    days = time.time() + 5 * 86_400
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit", reset_at=days)
    assert entry["source"] == "provider_reset"
    assert abs(entry["reset_at"] - days) < 1


def test_arm_ignores_an_implausible_provider_reset():
    entry = spc.arm_cooldown(_ROUTE, reason="overloaded", reset_at=1e20)
    assert entry is not None
    assert entry["source"] == "backoff"
    assert 59 <= entry["reset_at"] - entry["recorded_at"] <= 61


def test_arm_ignores_a_non_finite_provider_reset():
    entry = spc.arm_cooldown(_ROUTE, reason="overloaded", reset_at=float("inf"))
    assert entry is not None
    assert 59 <= entry["reset_at"] - entry["recorded_at"] <= 61
    assert entry["source"] == "backoff"


# ── Shared cooldown v2 probe and identity contracts ─────────────────────────────────────────

def test_expired_readers_claim_exactly_one_probe():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: spc.claim_probe(_ROUTE, entry["outage_id"]), range(8)))
    assert [token for token in tokens if token] and sum(bool(token) for token in tokens) == 1


def test_probe_lease_expiry_allows_takeover_and_wall_clock_is_clamped(monkeypatch):
    now = 1_900_000_000.0
    monkeypatch.setattr(spc.time, "time", lambda: now)
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = now - 1
        spc._write_state(path, state)
    first = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert first
    row = spc.read_cooldown(_ROUTE)
    assert row["probe_until"] <= now + spc._PROBE_LEASE_SECONDS
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["probe_until"] = now - 1
        spc._write_state(path, state)
    second = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert second and second != first


def test_failed_probe_uses_the_same_backoff_formula_as_rearming():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert spc._settle_probe(_ROUTE, entry["outage_id"], token, success=False)
    failed = spc.read_cooldown(_ROUTE)
    assert failed["backoff_count"] == 2
    assert 119 <= failed["reset_at"] - failed["recorded_at"] <= 121


def test_stale_probe_owner_cannot_clear_or_escalate_newer_outage():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["outage_id"] = "new-outage"
        row["probe_owner"] = "new-owner"
        spc._write_state(path, state)
    assert not spc._settle_probe(_ROUTE, entry["outage_id"], token, success=True)
    assert spc.read_cooldown(_ROUTE)["outage_id"] == "new-outage"


def test_probe_success_clears_alias_spellings_and_tombstone_prevents_flap(monkeypatch):
    monkeypatch.setattr(spc, "_custom_provider_for_url", lambda _url: "custom:proxy")
    route = ("custom", _ROUTE[1], _ROUTE[2])
    entry = spc.arm_cooldown(route, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider="custom:proxy", base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(("custom:proxy", _ROUTE[1], _ROUTE[2]), entry["outage_id"])
    assert spc._settle_probe(route, entry["outage_id"], token, success=True)
    assert spc.read_cooldown(route) is None


def test_custom_alias_merges_only_when_owner_is_unambiguous(monkeypatch):
    monkeypatch.setattr(spc, "_custom_provider_for_url", lambda _url: "custom:proxy")
    first = {**dict(provider="custom", base_url=_ROUTE[1], model="m", reset_at=time.time() + 60,
                      recorded_at=time.time(), backoff_count=1, outage_id="a", notice_claimed=False),
             "reason": "rate_limit"}
    second = {**first, "provider": "custom:proxy", "reset_at": time.time() + 120,
              "backoff_count": 3, "notice_claimed": True, "outage_id": "b"}
    path = spc._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "routes": {"old-a": first, "old-b": second}}))
    merged = spc.read_cooldown(("custom", _ROUTE[1], "m"))
    assert merged["provider"] == "custom:proxy"
    assert merged["backoff_count"] == 3 and merged["notice_claimed"] is True


def test_merged_loser_outage_can_still_settle_or_clear(monkeypatch):
    monkeypatch.setattr(spc, "_custom_provider_for_url", lambda _url: "custom:proxy")
    route = ("custom", _ROUTE[1], "merge-model")
    now = time.time()
    first = {**dict(provider="custom", base_url=_ROUTE[1], model=route[2], reset_at=now + 60,
                      recorded_at=now, backoff_count=1, outage_id="loser", notice_claimed=False),
             "reason": "rate_limit"}
    second = {**first, "provider": "custom:proxy", "reset_at": now + 120,
              "outage_id": "winner"}
    path = spc._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "routes": {"old-a": first, "old-b": second}}))
    assert spc.clear_if_current(route, "loser")
    assert spc.read_cooldown(route) is None


def test_reset_hint_precedence_and_malformed_values():
    from agent.error_classifier import _rate_limit_reset_seconds
    assert _rate_limit_reset_seconds("retry after 7s", {"resets_in_seconds": 30}, None) == 30
    assert _rate_limit_reset_seconds("retry after nonsense", {"reset_at": "bad"}, None) is None


def test_fallback_route_gets_independent_breaker_without_clearing_primary():
    agent = _agent_with_chain(_CHAIN)
    _activate(agent)
    primary = spc.read_cooldown(spc.route_from_agent(agent))
    assert primary is not None
    agent._fallback_activated = True
    agent._provider_fallback_active = True
    agent._rate_limit_backoff_count = 0
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit) is None
    assert spc.read_cooldown(spc.route_from_agent(agent))["outage_id"] == primary["outage_id"]
    fallback = getattr(agent, "_shared_fallback_cooldown_record", None)
    assert fallback and fallback["reason"] == "rate_limit"


def test_fallback_success_clears_only_the_live_fallback_breaker():
    agent = _agent_with_chain(_CHAIN)
    _activate(agent)
    primary_route = spc.route_from_agent(agent)
    primary = spc.read_cooldown(primary_route)
    agent._fallback_activated = True
    agent._provider_fallback_active = True
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit) is None
    fallback_route = spc.live_route_from_agent(agent)
    fallback = spc.read_cooldown(fallback_route)
    assert fallback is not None
    assert spc.complete_primary_recovery(agent)
    assert spc.read_cooldown(fallback_route) is None
    assert spc.read_cooldown(primary_route)["outage_id"] == primary["outage_id"]
    fresh = spc.arm_cooldown(fallback_route, reason="rate_limit")
    assert fresh["backoff_count"] == 1


def test_healthy_fallback_is_not_skipped_after_success(monkeypatch):
    from agent.chat_completion_helpers import _should_skip_fallback_candidate
    route = ("custom:healthy", "https://healthy.invalid/v1", "healthy-model")
    record = spc.arm_cooldown(route, reason="rate_limit")
    agent = SimpleNamespace(provider=route[0], model=route[2], base_url=route[1],
                            _fallback_activated=True, _shared_fallback_cooldown_record=record)
    assert spc.complete_primary_recovery(agent)
    entry = {"provider": route[0], "model": route[2], "base_url": route[1]}
    monkeypatch.setattr("agent.chat_completion_helpers._candidate_pool_exhausted", lambda *_args: False)
    monkeypatch.setattr("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", lambda *_args: None)
    monkeypatch.setattr("agent.backend_identity.should_skip_candidate", lambda *_args: False)
    assert not _should_skip_fallback_candidate(agent, entry, (route[0], route[2]), route[0], route[2], set())


def test_fresh_adopters_share_one_primary_probe_lease():
    """Fresh agents claim an expired outage before either is allowed to probe primary."""
    from agent import shared_primary_cooldown_runtime as shared_runtime
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)

    def fresh():
        return SimpleNamespace(
            _fallback_chain=[{"provider": "openai", "model": "fallback"}],
            provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2], _primary_runtime=None,
        )

    first, second = fresh(), fresh()
    with patch("agent.chat_completion_helpers.try_activate_fallback", return_value=True) as activate:
        assert shared_runtime._adopt_shared_primary_cooldown(first) is False
        assert shared_runtime._adopt_shared_primary_cooldown(second) is True
    assert getattr(first, "_shared_primary_probe_token", None)
    activate.assert_called_once()
    assert activate.call_args.kwargs["_shared_cooldown_record"]["outage_id"] == entry["outage_id"]


@pytest.mark.parametrize(
    ("hint_kind", "expected_source"),
    [
        ("float", "provider_reset"),
        ("milliseconds", "provider_reset"),
        ("iso8601", "provider_reset"),
        ("past", "backoff"),
        ("implausible", "backoff"),
    ],
)
def test_probe_failure_with_provider_reset_arms_until_reset(hint_kind, expected_source):
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    agent = SimpleNamespace(
        provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2], _primary_runtime=None,
        _shared_primary_cooldown_record=entry, _shared_primary_probe_token=token,
        _fallback_activated=False, _rate_limit_backoff_count=1,
    )
    reset_at = time.time() + 3600
    hints = {
        "float": reset_at,
        "milliseconds": str(round(reset_at * 1000)),
        "iso8601": datetime.fromtimestamp(reset_at, UTC).isoformat(),
        "past": str(time.time() - 1),
        "implausible": str(time.time() + spc._MAX_PROVIDER_RESET_SECONDS + 1),
    }
    hint = hints[hint_kind]
    assert spc._fail_agent_probe(agent, FailoverReason.rate_limit, hint)
    armed = _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit, reset_at=hint)
    current = spc.read_cooldown(_ROUTE)
    assert current["source"] == expected_source
    if expected_source == "provider_reset":
        assert abs(current["reset_at"] - reset_at) < 1
        assert armed is not None and 3500 < armed <= 3600
    else:
        assert 110 < current["reset_at"] - current["recorded_at"] < 130
        assert armed is not None and 110 < armed < 130
    assert agent._shared_primary_probe_token is None


def test_probe_failure_without_reset_keeps_exponential_backoff():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit", backoff_count=0)
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    agent = SimpleNamespace(
        _shared_primary_cooldown_record=entry, _shared_primary_probe_token=token,
    )
    started = time.time()
    assert spc._fail_agent_probe(agent, FailoverReason.rate_limit)
    current = spc.read_cooldown(_ROUTE)
    assert current["source"] == "backoff"
    assert 110 < current["reset_at"] - started < 130


def test_release_probe_accepts_a_merged_outage_id():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["merged_outage_ids"] = [entry["outage_id"]]
        row["outage_id"] = "canonical-outage"
        spc._write_state(path, state)
    assert spc._release_probe(_ROUTE, entry["outage_id"], token)
    released = spc.read_cooldown(_ROUTE)
    assert released["state"] == "expired"
    assert "probe_owner" not in released


def test_rearm_preserves_merged_outage_and_probe_metadata():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["merged_outage_ids"] = ["merged-loser"]
        row["probe_owner"] = "probe-token"
        row["probe_until"] = time.time() + 60
        row["state"] = "probing"
        spc._write_state(path, state)
    rearmed = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    assert rearmed["outage_id"] == entry["outage_id"]
    assert rearmed["merged_outage_ids"] == ["merged-loser"]
    assert rearmed["probe_owner"] == "probe-token"
    assert rearmed["probe_until"] > time.time()
    assert rearmed["state"] == "probing"


def test_claim_probe_accepts_a_merged_outage_id():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        row["outage_id"] = "canonical-outage"
        row["merged_outage_ids"] = [entry["outage_id"]]
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert token


def test_claim_probe_distinguishes_tombstone_from_owned_probe():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    first = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert first
    assert spc.claim_probe(_ROUTE, entry["outage_id"]).kind is spc.ProbeClaimKind.OWNED_ELSEWHERE
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["state"] = "tombstone"
        row.pop("probe_owner", None)
        row.pop("probe_until", None)
        spc._write_state(path, state)
    assert spc.claim_probe(_ROUTE, entry["outage_id"]).kind is spc.ProbeClaimKind.CLEARED


def test_builtin_fallback_lookup_uses_provider_default_base_url():
    from agent.shared_primary_probe_runtime import fallback_route_is_active
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=AssertionError("fallback lookup must not resolve credentials")):
        spc.arm_cooldown(("openrouter", "https://openrouter.ai/api/v1", "x/model"), reason="rate_limit")
        assert fallback_route_is_active("openrouter", None, "x/model")


def test_configured_fallback_url_requires_matching_model():
    from agent.shared_primary_probe_runtime import _configured_base_url

    config = {
        "model": {
            "provider": "openrouter",
            "model": "primary-model",
            "base_url": "https://stale-model-url.invalid/v1",
        },
        "providers": {
            "openrouter": {"api": "https://provider-default.invalid/v1"},
        },
    }
    with patch("hermes_cli.config.load_config_readonly", return_value=config):
        assert _configured_base_url("openrouter", "fallback-model") == "https://provider-default.invalid/v1"


def test_probe_failure_flag_is_scoped_to_its_outage():
    """A later outage on the same agent must arm after an earlier probe failure."""
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    agent = SimpleNamespace(
        provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2], _primary_runtime=None,
        _shared_primary_cooldown_record=entry, _shared_primary_probe_token=token,
        _fallback_activated=False, _rate_limit_backoff_count=0, _fallback_episode_cooled=False,
    )
    assert spc._fail_agent_probe(agent)
    agent._fallback_episode_cooled = False
    first = _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit)
    assert first is not None
    assert agent._fallback_episode_cooled is True
    old_outage = agent._shared_primary_cooldown_record["outage_id"]
    spc.clear_if_current(_ROUTE, old_outage)
    agent._shared_primary_cooldown_record = None
    second = _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit)
    assert second is not None
    assert agent._shared_primary_cooldown_record["outage_id"] != old_outage


def test_fallback_breaker_does_not_inherit_primary_backoff_count():
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    agent = _agent_with_chain(_CHAIN)
    _activate(agent)
    agent._fallback_activated = True
    agent._provider_fallback_active = True
    agent._rate_limit_backoff_count = 7
    assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit) is None
    assert agent._shared_fallback_cooldown_record["backoff_count"] == 1


@pytest.mark.parametrize("hint_kind", ["float", "milliseconds", "iso8601"])
def test_fallback_breaker_normalizes_provider_reset_hints(hint_kind, monkeypatch):
    """Fallback-route arming accepts every provider reset representation."""
    from agent.fallback_cooldown import _arm_rate_limit_cooldown

    agent = _agent_with_chain(_CHAIN)
    _activate(agent)
    # Millisecond hints round up by up to 0.5 ms; do not race that precision
    # against a sub-millisecond cached-config arming path.
    monkeypatch.setattr(time, "time", lambda: 1_900_000_000.0)
    reset_at = time.time() + 3600
    hints = {
        "float": reset_at,
        "milliseconds": str(round(reset_at * 1000)),
        "iso8601": datetime.fromtimestamp(reset_at, UTC).isoformat(),
    }
    agent._fallback_activated = True
    agent._provider_fallback_active = True
    assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit, reset_at=hints[hint_kind]) is None
    record = agent._shared_fallback_cooldown_record
    assert record["source"] == "provider_reset"
    assert 3500 < record["reset_at"] - record["recorded_at"] <= 3600


def test_fallback_cooldown_lookup_uses_resolved_base_url():
    from agent.shared_primary_probe_runtime import fallback_route_is_active
    with patch("hermes_cli.config.load_config_readonly", return_value={
        "providers": {"resolved": {"base_url": "https://resolved.invalid/v1"}},
    }), patch("hermes_cli.runtime_provider.resolve_runtime_provider",
              side_effect=AssertionError("fallback cooldown lookup must not resolve credentials")):
        spc.arm_cooldown(("custom:resolved", "https://resolved.invalid/v1", "model"), reason="rate_limit")
        assert fallback_route_is_active("custom:resolved", None, "model")


def test_non_outage_probe_error_releases_without_escalating():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    agent = SimpleNamespace(_shared_primary_cooldown_record=entry, _shared_primary_probe_token=token)
    assert spc._fail_agent_probe(agent, FailoverReason.context_overflow)
    released = spc.read_cooldown(_ROUTE)
    assert released["state"] == "expired"
    assert released["backoff_count"] == entry["backoff_count"]


def test_interrupt_releases_probe_without_escalating():
    from agent.turn_api_call import handle_api_interrupt
    agent = SimpleNamespace(
        _shared_primary_probe_token="probe-token", thinking_callback=None,
        _has_pending_redirect=lambda: True,
        clear_interrupt=lambda preserve_redirect: True,
    )
    retry = SimpleNamespace()
    with patch("agent.shared_primary_probe_runtime.release_primary_probe") as release:
        verdict = handle_api_interrupt(
            agent, _retry=retry, thinking_spinner=None, messages=[], conversation_history=[],
            api_start_time=time.time(), interrupted=False, final_response=None,
        )
    assert verdict.action == "break"
    release.assert_called_once_with(agent, "probe-token")


def test_adoption_claim_race_rechecks_the_surviving_record():
    from agent import shared_primary_cooldown_runtime as shared_runtime
    record = {**spc.arm_cooldown(_ROUTE, reason="rate_limit"), "state": "expired", "reset_at": time.time() - 1}
    agent = SimpleNamespace(_fallback_chain=[{"provider": "openai", "model": "fallback"}],
                            provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])
    current = {**record, "outage_id": "new-outage"}
    with patch("agent.shared_primary_cooldown.claim_probe", return_value=spc.ProbeClaimOutcome(spc.ProbeClaimKind.REPLACED, record=current)), \
         patch("agent.chat_completion_helpers.try_activate_fallback", return_value=True) as activate:
        # The first read is the adoption snapshot, the second is the race re-read.
        with patch("agent.shared_primary_cooldown.read_cooldown", side_effect=[record, current]):
            assert shared_runtime._adopt_shared_primary_cooldown(agent)
    assert activate.call_args.kwargs["_shared_cooldown_record"]["outage_id"] == "new-outage"


def test_adoption_clears_stale_record_and_probe_token_when_state_is_gone():
    from agent import shared_primary_cooldown_runtime as shared_runtime

    agent = SimpleNamespace(
        _fallback_chain=[{"provider": "openai", "model": "fallback"}],
        provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2],
        _shared_primary_cooldown_record={"outage_id": "stale"},
        _shared_primary_probe_token="stale-token",
    )
    with patch("agent.shared_primary_cooldown.read_cooldown", return_value=None):
        assert shared_runtime._adopt_shared_primary_cooldown(agent) is False
    assert agent._shared_primary_cooldown_record is None
    assert agent._shared_primary_probe_token is None


def test_primary_probe_distinguishes_cleared_state_from_owned_probe():
    from agent.shared_primary_probe_runtime import claim_primary_probe

    record = {"provider": _ROUTE[0], "base_url": _ROUTE[1], "model": _ROUTE[2], "outage_id": "outage"}
    agent = SimpleNamespace(_shared_primary_cooldown_record=record, _shared_primary_probe_token="old-token")
    with patch("agent.shared_primary_cooldown.claim_probe", return_value=spc.ProbeClaimOutcome(spc.ProbeClaimKind.OWNED_ELSEWHERE)):
        assert claim_primary_probe(agent, "expired").kind is spc.ProbeClaimKind.OWNED_ELSEWHERE
    with patch("agent.shared_primary_cooldown.claim_probe", return_value=spc.ProbeClaimOutcome(spc.ProbeClaimKind.CLEARED)):
        assert claim_primary_probe(agent, "expired").kind is spc.ProbeClaimKind.CLEARED
    assert agent._shared_primary_cooldown_record is None
    assert agent._shared_primary_probe_token is None


@pytest.mark.parametrize(
    ("kind", "allows_primary"),
    [
        (spc.ProbeClaimKind.CLAIMED, True),
        (spc.ProbeClaimKind.OWNED_ELSEWHERE, False),
        (spc.ProbeClaimKind.STILL_COOLING, False),
        (spc.ProbeClaimKind.REPLACED, False),
        (spc.ProbeClaimKind.CLEARED, True),
        (spc.ProbeClaimKind.UNAVAILABLE, False),
    ],
)
def test_restore_primary_runtime_handles_every_probe_outcome(kind, allows_primary):
    agent = _agent_with_chain(_CHAIN)
    _activate(agent)
    agent._rate_limited_until = 0
    agent._fallback_activated = True
    agent._provider_fallback_active = True
    _age_record(spc.route_from_agent(agent), reset_ago=1, window=60)
    outcome = spc.ProbeClaimOutcome(
        kind, token="probe-token" if kind is spc.ProbeClaimKind.CLAIMED else None,
        record=agent._shared_primary_cooldown_record if kind is spc.ProbeClaimKind.REPLACED else None,
    )
    with patch("agent.shared_primary_cooldown.claim_probe", return_value=outcome), \
         patch("agent.route_binding.reinstall_primary_runtime"), \
         patch("agent.fallback_notice.finish_fallback_restore"):
        restored = agent._restore_primary_runtime()
    assert restored is allows_primary


def test_claim_probe_reports_still_cooling_and_unavailable_explicitly():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit", reset_at=time.time() + 3600)
    assert spc.claim_probe(_ROUTE, entry["outage_id"]).kind is spc.ProbeClaimKind.STILL_COOLING
    with patch("agent.shared_primary_cooldown._locked_state", side_effect=OSError("unreadable")):
        outcome = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert outcome.kind is spc.ProbeClaimKind.UNAVAILABLE


def test_same_primary_recovery_keeps_probe_lease_and_settles_once():
    from agent.fallback_cooldown import _arm_rate_limit_cooldown

    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    claimed = spc.claim_probe(_ROUTE, entry["outage_id"])
    agent = SimpleNamespace(
        provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2], _primary_runtime=None,
        _shared_primary_cooldown_record=entry, _shared_primary_probe_token=claimed.token,
        _fallback_activated=False,
    )
    assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit) == 0
    assert spc.claim_probe(_ROUTE, entry["outage_id"]).kind is spc.ProbeClaimKind.OWNED_ELSEWHERE
    assert spc.settle_primary_probe_on_exit(agent, reason="success")
    assert spc.settle_primary_probe_on_exit(agent, reason="success") is False
    assert spc.read_cooldown(_ROUTE) is None


def test_429_after_credential_rotation_does_not_double_shared_backoff():
    from agent.fallback_cooldown import _arm_rate_limit_cooldown

    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    claimed = spc.claim_probe(_ROUTE, entry["outage_id"])
    agent = SimpleNamespace(
        _shared_primary_cooldown_record=entry, _shared_primary_probe_token=claimed.token,
        _fallback_activated=False,
    )
    assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit) == 0
    assert spc.settle_primary_probe_on_exit(agent, reason=FailoverReason.rate_limit)
    failed = spc.read_cooldown(_ROUTE)
    assert failed["backoff_count"] == entry["backoff_count"] + 1
    assert spc.settle_primary_probe_on_exit(agent, reason=FailoverReason.rate_limit) is False
    assert spc.read_cooldown(_ROUTE)["backoff_count"] == failed["backoff_count"]


def test_context_overflow_compression_retry_keeps_probe_lease_from_second_agent():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    first = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert first.kind is spc.ProbeClaimKind.CLAIMED
    second = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert second.kind is spc.ProbeClaimKind.OWNED_ELSEWHERE
    assert spc._release_probe(_ROUTE, entry["outage_id"], first.token)


def test_each_transport_attempt_refreshes_tombstone_timestamp(monkeypatch):
    from agent.turn_api_call import perform_api_call
    times = iter((10.0, 20.0))
    monkeypatch.setattr("agent.turn_api_call.time.time", lambda: next(times))
    attempts = []
    agent = SimpleNamespace(
        _interruptible_streaming_api_call=lambda _kwargs, on_first_delta: attempts.append(agent._current_api_request_started_at) or object(),
        _interruptible_api_call=None, _get_transport=lambda: None, _is_codex_backend=lambda: False,
        _has_stream_consumers=lambda: True, api_mode="chat_completions", base_url="https://primary.invalid/v1",
        provider="custom", model="primary", session_id="s", platform="cli", _model_request_active=None,
        thinking_callback=None, _pending_redirect_lock=None, _pending_redirect=False,
        _has_pending_redirect=lambda: False,
    )
    with patch("agent.turn_api_call._should_stream", return_value=True), \
         patch("hermes_cli.middleware.run_llm_execution_middleware",
               side_effect=lambda _request, next_call, **_kwargs: (next_call(_request), next_call(_request))[1]):
        perform_api_call(
            agent, api_kwargs={}, _original_api_kwargs={}, _llm_middleware_trace=[],
            _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
            retry_count=0, api_call_count=1, api_request_id="r", effective_task_id="t",
            turn_id="turn", interrupted=False,
        )
    assert attempts == [10.0, 20.0]


def test_local_probe_release_does_not_escalate_backoff():
    from agent.shared_primary_probe_runtime import release_primary_probe
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    agent = SimpleNamespace(_shared_primary_cooldown_record=entry, _shared_primary_probe_token=token)
    release_primary_probe(agent, token)
    released = spc.read_cooldown(_ROUTE)
    assert released["state"] == "expired"
    assert released["backoff_count"] == entry["backoff_count"]


def test_probe_release_logs_suppressed_exception(caplog):
    from agent.shared_primary_probe_runtime import release_primary_probe

    entry = {"provider": _ROUTE[0], "base_url": _ROUTE[1], "model": _ROUTE[2], "outage_id": "outage"}
    agent = SimpleNamespace(_shared_primary_cooldown_record=entry, _shared_primary_probe_token="token")
    with patch("agent.shared_primary_cooldown._release_probe", side_effect=RuntimeError("release failed")):
        with caplog.at_level(logging.DEBUG, logger="agent.shared_primary_probe_runtime"):
            release_primary_probe(agent, "token")
    assert "Could not release primary cooldown probe" in caplog.text


def test_fresh_recovery_tombstone_rejects_late_failure():
    entry = spc.arm_cooldown(_ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        row = state["routes"][spc.route_key(provider=_ROUTE[0], base_url=_ROUTE[1], model=_ROUTE[2])]
        row["reset_at"] = time.time() - 1
        spc._write_state(path, state)
    token = spc.claim_probe(_ROUTE, entry["outage_id"])
    assert spc._settle_probe(_ROUTE, entry["outage_id"], token, success=True)
    assert spc.arm_cooldown(_ROUTE, reason="rate_limit", request_started_at=entry["recorded_at"]) is None


def test_stream_first_delta_does_not_announce_recovery():
    from agent.turn_api_call import perform_api_call
    with patch("agent.turn_api_call._should_stream", return_value=True), \
         patch("agent.shared_primary_cooldown.complete_primary_recovery") as recovered:
        agent = SimpleNamespace(
            _interruptible_streaming_api_call=lambda _kwargs, on_first_delta: (
                on_first_delta(), (_ for _ in ()).throw(RuntimeError("late stream failure"))
            ),
            _interruptible_api_call=None, _get_transport=lambda: None, _is_codex_backend=lambda: False,
            _has_stream_consumers=lambda: True, api_mode="chat_completions", base_url="https://primary.invalid/v1",
            provider="custom", model="primary", session_id="s", platform="cli", _model_request_active=None,
            thinking_callback=None,
            _pending_redirect_lock=None, _pending_redirect=False, _has_pending_redirect=lambda: False,
        )
        with pytest.raises(RuntimeError, match="late stream failure"):
            perform_api_call(
                agent, api_kwargs={}, _original_api_kwargs={}, _llm_middleware_trace=[],
                _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
                retry_count=0, api_call_count=1, api_request_id="r", effective_task_id="t",
                turn_id="turn", interrupted=False,
            )
    recovered.assert_not_called()
