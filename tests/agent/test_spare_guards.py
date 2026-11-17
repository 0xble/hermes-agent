"""Shared spare-fallback admission and daily-budget guards."""

from types import SimpleNamespace

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
