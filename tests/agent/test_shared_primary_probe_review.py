"""Lost-lease and long-request regressions from the shared cooldown review (#1246)."""
from types import SimpleNamespace

import pytest

from agent import shared_primary_cooldown as spc
from agent.error_classifier import FailoverReason
from agent.fallback_cooldown import _arm_rate_limit_cooldown
from agent.shared_primary_probe_runtime import claim_primary_probe

ROUTE = ("custom:review", "https://review.invalid/v1", "primary")


def _claim_agent():
    record = spc.arm_cooldown(ROUTE, reason="rate_limit")
    with spc._locked_state() as (path, state):
        key = spc.route_key(provider=ROUTE[0], base_url=ROUTE[1], model=ROUTE[2])
        state["routes"][key]["reset_at"] = spc.time.time() - 1
        spc._write_state(path, state)
    claim = spc.claim_probe(ROUTE, record["outage_id"])
    assert claim.kind is spc.ProbeClaimKind.CLAIMED
    return SimpleNamespace(
        provider=ROUTE[0], base_url=ROUTE[1], model=ROUTE[2], _primary_runtime=None,
        _shared_primary_cooldown_record=record, _shared_primary_probe_token=claim.token,
        _fallback_activated=False,
    )


@pytest.mark.parametrize("reason", ["rate_limit", "success", "release", "interrupt"])
def test_operator_clear_drops_token_on_failed_probe_exit(reason):
    agent = _claim_agent()
    assert spc.clear_cooldowns(all_routes=True)
    assert not spc.settle_primary_probe_on_exit(agent, reason=reason)
    assert agent._shared_primary_probe_token is None
    assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit) > 0
    assert spc.read_cooldown(ROUTE) is not None


@pytest.mark.parametrize("exit_first", [False, True])
def test_expired_owner_can_rearm_after_takeover_recovers(monkeypatch, exit_first):
    now = 1_900_000_000.0
    monkeypatch.setattr(spc.time, "time", lambda: now)
    agent = _claim_agent()
    now += spc._PROBE_LEASE_SECONDS + 1
    second = spc.claim_probe(ROUTE, agent._shared_primary_cooldown_record["outage_id"])
    assert second.kind is spc.ProbeClaimKind.CLAIMED
    assert spc._settle_probe(ROUTE, second.record["outage_id"], second.token, success=True)
    if exit_first:
        assert not spc.settle_primary_probe_on_exit(agent, reason="rate_limit")
    now += spc._RECOVERY_TOMBSTONE_GRACE_SECONDS + 1
    for _ in range(3):
        assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit) > 0
        assert agent._shared_primary_probe_token is None
        assert spc.read_cooldown(ROUTE) is not None


@pytest.mark.parametrize("shared_state", ["absent", "active", "unavailable"])
def test_claim_early_return_discards_lost_token(shared_state):
    agent = _claim_agent()
    spc.clear_cooldowns(all_routes=True)
    claim_primary_probe(agent, shared_state)
    assert agent._shared_primary_probe_token is None


def test_lost_owner_does_not_overwrite_the_new_probe():
    agent = _claim_agent()
    with spc._locked_state() as (path, state):
        key = spc.route_key(provider=ROUTE[0], base_url=ROUTE[1], model=ROUTE[2])
        state["routes"][key]["probe_owner"] = "new-owner"
        state["routes"][key]["outage_id"] = "new-outage"
        spc._write_state(path, state)
    assert not spc.settle_primary_probe_on_exit(agent, reason="release")
    assert agent._shared_primary_probe_token is None
    assert _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit) is None
    assert spc.read_cooldown(ROUTE)["probe_owner"] == "new-owner"


@pytest.mark.parametrize("all_routes", [False, True])
def test_operator_clear_preserves_tombstone_and_does_not_report_it(all_routes):
    agent = _claim_agent()
    record = agent._shared_primary_cooldown_record
    assert spc.settle_primary_probe_on_exit(agent, reason="success")
    assert spc.clear_cooldowns(None if all_routes else ROUTE[2], all_routes=all_routes) == []
    assert spc.arm_cooldown(ROUTE, reason="rate_limit", request_started_at=record["recorded_at"]) is None


def test_custom_route_resolution_caches_config_scan_per_home_and_revision(tmp_path, monkeypatch):
    import hermes_cli.config as config_module
    from hermes_constants import get_hermes_home

    url = "https://cache-review.invalid/v1"
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.yaml"
    config_path.write_text(f"providers:\n  one:\n    name: One\n    api: {url}\n", encoding="utf-8")
    original = config_module.load_config_readonly
    reads = []

    def counted():
        reads.append(get_hermes_home())
        return original()

    monkeypatch.setattr(config_module, "load_config_readonly", counted)
    route = ("custom", url, "cached-model")
    first = spc.arm_cooldown(route, reason="rate_limit")
    for _ in range(3):
        assert spc.read_cooldown(route)["provider"] == first["provider"]
    assert len(reads) == 1
    config_path.write_text(f"providers:\n  two:\n    name: Two\n    api: {url}\n", encoding="utf-8")
    second = spc.canonical_route(route)
    assert second[0] != first["provider"]
    other = tmp_path / "other-home"
    other.mkdir()
    (other / "config.yaml").write_text(f"providers:\n  three:\n    name: Three\n    api: {url}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert spc.canonical_route(route)[0] not in {first["provider"], second[0]}
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert spc.canonical_route(route) == second
    assert len(reads) == 3


@pytest.mark.parametrize("entrypoint", ["restore", "adopt"])
def test_runtime_probe_lease_covers_long_request_timeout(monkeypatch, entrypoint):
    from agent.shared_primary_cooldown_runtime import _adopt_shared_primary_cooldown

    now = 1_900_000_000.0
    monkeypatch.setattr(spc.time, "time", lambda: now)
    agent = _claim_agent()
    assert spc.settle_primary_probe_on_exit(agent, reason="release")
    agent._fallback_chain = [{"provider": "openai", "model": "fallback"}]
    monkeypatch.setattr("hermes_cli.timeouts.get_provider_request_timeout", lambda *_args: 1800.0)
    if entrypoint == "restore":
        assert claim_primary_probe(agent, "expired").kind is spc.ProbeClaimKind.CLAIMED
    else:
        assert not _adopt_shared_primary_cooldown(agent)
    current = spc.read_cooldown(ROUTE)
    assert current["probe_until"] >= now + 1800
    now += 900  # longer than both the old lease and the expired-record pruning grace
    assert spc.claim_probe(ROUTE, current["outage_id"]).kind is spc.ProbeClaimKind.OWNED_ELSEWHERE
    assert spc.read_cooldown(ROUTE)["probe_owner"] == agent._shared_primary_probe_token
