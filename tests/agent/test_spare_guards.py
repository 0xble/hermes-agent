"""Shared spare-fallback admission and daily-budget guards."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent.spare_guards as guards


def _agent():
    return SimpleNamespace(_spare_fallback_lease=None, _spare_fallback_upstream=None,
                           _provider_fallback_active=False)


def _route(model="muse-1", **extra):
    return {"provider": "custom", "model": model, **extra}


def _config(cap=2, budget=30_000_000):
    return {"delegation": {"spare_upstreams": ["meta", "xai"],
                            "max_concurrent_on_spare": cap},
            "fallback_budgets": {"meta": budget, "xai": budget}}


def test_spare_cap_and_lease_release(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config(cap=2))
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: True)
    first, second, third = _agent(), _agent(), _agent()

    assert guards.reserve_fallback_route(first, _route())
    assert guards.reserve_fallback_route(second, _route())
    assert not guards.reserve_fallback_route(third, _route())
    guards.release_fallback_route(first)
    assert guards.reserve_fallback_route(third, _route())
    guards.release_fallback_route(second)
    guards.release_fallback_route(third)


def test_skip_when_daily_budget_is_exhausted(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config(budget=10))
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: True)
    monkeypatch.setattr(guards, "_local_day", lambda: "2026-11-16")
    agent = _agent()
    agent._provider_fallback_active = True
    agent._spare_fallback_upstream = "meta"
    guards.record_fallback_usage(agent, SimpleNamespace(input_tokens=10, cache_read_tokens=0, output_tokens=0))
    assert guards.used_today("meta") == 10
    assert not guards.reserve_fallback_route(_agent(), _route())


def test_primary_spare_is_not_capped_or_budgeted(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config(cap=1, budget=1))
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: False)
    agent = _agent()
    # Direct primary routes never call reserve_fallback_route, and usage accounting is inactive
    # until a fallback is set.
    agent._spare_fallback_upstream = None
    agent._provider_fallback_active = False
    guards.record_fallback_usage(agent, SimpleNamespace(input_tokens=100, cache_read_tokens=100, output_tokens=100))
    assert guards.used_today("meta") == 0


def test_primary_spare_route_is_exempt_from_child_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config(cap=1, budget=1))
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: False)
    first, second = _agent(), _agent()
    assert guards.reserve_fallback_route(first, _route())
    assert guards.reserve_fallback_route(second, _route())


def test_real_try_activate_skips_capped_spare_and_moves_down_chain(monkeypatch, tmp_path):
    """The real fallback walker skips a third delegated child over the spare cap.

    The next non-spare route remains eligible, and releasing the first child's lease makes
    the spare eligible again.  This exercises ``try_activate_fallback`` rather than only the
    admission helper.
    """
    from run_agent import AIAgent

    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config(cap=2))
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: True)
    client = MagicMock()
    client.base_url = "https://openrouter.ai/api/v1"
    client.api_key = "fallback-key"
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, "resolved")),
    ):
        agents = [
            AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                    skip_context_files=True, skip_memory=True,
                    fallback_model=[{"provider": "openrouter", "model": "muse-1"}])
            for _ in range(3)
        ]
        for agent in agents:
            agent.client = client
        assert agents[0]._try_activate_fallback()
        assert agents[1]._try_activate_fallback()
        agents[2]._fallback_chain.append({"provider": "openai", "model": "gpt-4o"})
        assert agents[2]._try_activate_fallback()
        assert agents[2].provider == "openai"
        guards.release_fallback_route(agents[0])
        replacement = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                              skip_context_files=True, skip_memory=True,
                              fallback_model=[{"provider": "openrouter", "model": "muse-3"}])
        replacement.client = client
        assert replacement._try_activate_fallback()
        guards.release_fallback_route(agents[1])
        guards.release_fallback_route(replacement)


def test_pid_dead_and_expired_leases_are_pruned(monkeypatch):
    state = {"leases": {"meta": {
        "dead": {"pid": 10, "expires_at": 999.0},
        "expired": {"pid": os.getpid(), "expires_at": 1.0},
        "live": {"pid": os.getpid(), "expires_at": 999.0},
    }}, "usage": {}}
    monkeypatch.setattr(guards, "_pid_alive", lambda pid: pid != 10)
    guards._prune(state, now=100.0, day="2026-11-16")
    assert set(state["leases"]["meta"]) == {"live"}


def test_corrupt_state_and_infinity_fail_open(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"leases":{"meta":{"bad":{"pid":1,"expires_at":Infinity}}},'
        '"usage":{"2026-11-16":{"meta":Infinity}}}', encoding="utf-8",
    )
    monkeypatch.setattr(guards, "_state_path", lambda: path)
    monkeypatch.setattr(guards, "_local_day", lambda: "2026-11-16")
    monkeypatch.setattr(guards, "_load_config", lambda: _config())
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: True)
    assert guards.used_today("meta") == 0
    assert guards.reserve_fallback_route(_agent(), _route())
    assert guards.spare_settings({"fallback_budgets": {"meta": float("inf")}})[2]["meta"] == 30_000_000


def test_lock_oserror_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config())
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: True)
    monkeypatch.setattr(guards, "_locked_state", lambda: (_ for _ in ()).throw(OSError("lock")))
    assert guards.reserve_fallback_route(_agent(), _route())


def test_failed_bind_rolls_back_only_new_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config(cap=2))
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: True)
    agent = _agent()
    prior = guards.reserve_fallback_route(agent, _route("muse-prior"))
    assert prior and agent._spare_fallback_lease
    next_reservation = guards.reserve_fallback_route(agent, _route("grok-next"))
    assert next_reservation and next_reservation.newly_acquired
    guards.release_fallback_route(agent, _lease=next_reservation)
    assert agent._spare_fallback_lease == prior.lease
    assert guards.used_today("meta") == 0
    guards.release_fallback_route(agent)


def test_cap_refusal_follows_exhausted_chain_without_sleep(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config(cap=1))
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: True)
    holder = _agent()
    assert guards.reserve_fallback_route(holder, _route())
    refused = _agent()
    assert guards.reserve_fallback_route(refused, _route()) is None
    guards.release_fallback_route(holder)


def test_daily_budget_rolls_over(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(guards, "_load_config", lambda: _config(budget=10))
    monkeypatch.setattr(guards, "_in_delegated_child", lambda: True)
    day = {"value": "2026-11-16"}
    monkeypatch.setattr(guards, "_local_day", lambda: day["value"])
    agent = _agent()
    agent._provider_fallback_active = True
    agent._spare_fallback_upstream = "meta"
    guards.record_fallback_usage(agent, SimpleNamespace(input_tokens=10, cache_read_tokens=0, output_tokens=0))
    assert guards.used_today("meta") == 10
    day["value"] = "2026-11-17"
    assert guards.reserve_fallback_route(_agent(), _route())
    assert guards.used_today("meta") == 0
