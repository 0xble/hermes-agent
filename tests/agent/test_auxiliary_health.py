from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from agent import auxiliary_health as health


def _identity(home: str = "/profile/a", *, provider: str = "openrouter", endpoint: str = "", model: str = ""):
    return health.AuxiliaryRouteIdentity(home, provider, endpoint, model)


@pytest.fixture
def state_path(monkeypatch, tmp_path):
    path = tmp_path / "state" / "auxiliary_route_health.json"
    monkeypatch.setattr(health, "_state_path", lambda: path)
    monkeypatch.setattr(health, "_lock_path", lambda: path.with_name(".auxiliary_route_health.lock"))
    return path


def test_health_is_typed_and_scoped_by_route(state_path):
    identity = _identity(endpoint="https://example.test/v1", model="model-a")

    record = health.mark_unhealthy(identity, ttl=30, reason="payment error")

    assert record is not None
    assert record.lane is health.AuxiliaryHealthLane.QUOTA
    assert health.read_health(identity) == record
    assert health.read_health(_identity(endpoint="https://other.test/v1", model="model-a")) is None
    assert health.read_health(_identity(endpoint="https://example.test/v1", model="model-b")) is None
    assert state_path.stat().st_mode & 0o077 == 0


def test_health_does_not_share_parser_failures(state_path):
    identity = _identity()

    assert health.mark_unhealthy(identity, ttl=30, reason="invalid provider response") is None
    assert health.read_health(identity) is None


def test_health_expires_and_clears(state_path, monkeypatch):
    identity = _identity()
    now = [100.0]
    monkeypatch.setattr(health.time, "time", lambda: now[0])

    health.mark_unhealthy(identity, ttl=10, reason="rate limit")
    assert health.read_health(identity).lane is health.AuxiliaryHealthLane.RATE_LIMIT
    now[0] = 111.0
    assert health.read_health(identity) is None
    health.clear_health(identity)
    assert health.read_health(identity) is None


def test_health_preserves_same_lane_epoch_and_increments_on_new_lane(state_path):
    identity = _identity()

    first = health.mark_unhealthy(identity, ttl=30, reason="provider unavailable")
    second = health.mark_unhealthy(identity, ttl=60, reason="provider unavailable")
    third = health.mark_unhealthy(identity, ttl=30, reason="rate limit")

    assert first.epoch == second.epoch
    assert third.epoch == second.epoch + 1
    assert third.expires_at >= second.expires_at


def test_malformed_persisted_epoch_fails_open(state_path):
    identity = _identity()
    health.mark_unhealthy(identity, ttl=30, reason="rate limit")
    raw_key = health._record_key(identity.as_key())
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["routes"][raw_key]["epoch"] = "oops"
    state["routes"][raw_key]["notice_epoch"] = -1
    state_path.write_text(json.dumps(state), encoding="utf-8")

    record = health.mark_unhealthy(identity, ttl=30, reason="rate limit")

    assert record is not None
    assert record.epoch == 0
    assert health.claim_notice(identity) is True


def test_expired_record_notice_is_claimed_once(state_path, monkeypatch):
    identity = _identity()
    now = [100.0]
    monkeypatch.setattr(health.time, "time", lambda: now[0])
    health.mark_unhealthy(identity, ttl=0, reason="rate limit")

    assert health.claim_notice(identity) is True
    assert health.claim_notice(identity) is False


def test_corrupt_state_is_quarantined_without_pinning_route(state_path):
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not-json", encoding="utf-8")

    assert health.read_health(_identity()) is None
    assert not state_path.exists()
    assert list(state_path.parent.glob("auxiliary_route_health.json.corrupt-*"))


def _probe_in_child(state_path_text: str, result_path_text: str) -> None:
    from agent import auxiliary_health as child_health
    from agent import auxiliary_client as child_aux

    path = Path(state_path_text)
    child_health._state_path = lambda: path
    child_health._lock_path = lambda: path.with_name(".auxiliary_route_health.lock")
    healthy = child_aux._is_provider_unhealthy("custom", "https://shared.example/v1")
    Path(result_path_text).write_text("true" if healthy else "false", encoding="utf-8")


def test_auxiliary_caller_observes_health_after_process_restart(state_path, tmp_path):
    from agent import auxiliary_client as aux

    aux._reset_aux_unhealthy_cache()
    aux._mark_provider_unhealthy(
        "custom", base_url="https://shared.example/v1", ttl=30, reason="payment error",
    )
    assert aux._is_provider_unhealthy("custom", "https://shared.example/v1") is True

    result_path = tmp_path / "probe.txt"
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_probe_in_child, args=(str(state_path), str(result_path)))
    process.start()
    process.join(10)
    assert process.exitcode == 0
    assert result_path.read_text(encoding="utf-8") == "true"
    aux._reset_aux_unhealthy_cache()


def _mark_in_child(state_path_text: str, provider: str) -> None:
    from agent import auxiliary_health as child_health

    path = Path(state_path_text)
    child_health._state_path = lambda: path
    child_health._lock_path = lambda: path.with_name(".auxiliary_route_health.lock")
    child_health.mark_unhealthy(_identity(provider=provider), ttl=30, reason="rate limit")


def test_same_state_file_merges_concurrent_route_marks(state_path):
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(target=_mark_in_child, args=(str(state_path), provider))
        for provider in ("openrouter", "nous", "anthropic")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["routes"]) == 3


def test_expired_route_allows_one_probe_then_recovery_clears_shared_state(state_path, monkeypatch):
    from agent import auxiliary_client as aux

    identity = aux._auxiliary_route_identity("openrouter")
    now = [100.0]
    monkeypatch.setattr(health.time, "time", lambda: now[0])
    aux._reset_aux_unhealthy_cache()
    aux._mark_provider_unhealthy("openrouter", ttl=10, reason="rate limit")
    aux._clear_provider_health("openrouter")
    assert health.read_health(identity) is not None

    now[0] = 111.0
    assert aux._is_provider_unhealthy("openrouter") is False
    assert aux._candidate_is_unhealthy("openrouter") is False
    assert aux._candidate_is_unhealthy("openrouter") is True

    aux._clear_provider_health("openrouter")
    assert health.read_health(identity) is None
    assert aux._is_provider_unhealthy("openrouter") is False
    aux._reset_aux_unhealthy_cache()


def _claim_notice_in_child(state_path_text: str, result_path_text: str, epoch: int) -> None:
    from agent import auxiliary_health as child_health

    path = Path(state_path_text)
    child_health._state_path = lambda: path
    child_health._lock_path = lambda: path.with_name(".auxiliary_route_health.lock")
    result = child_health.claim_notice(_identity(provider="openrouter"), epoch)
    Path(result_path_text).write_text("true" if result else "false", encoding="utf-8")


def test_notice_claim_is_deduplicated_across_processes(state_path, tmp_path):
    identity = _identity(provider="openrouter")
    record = health.mark_unhealthy(identity, ttl=30, reason="payment error")
    assert record is not None
    ctx = multiprocessing.get_context("spawn")
    paths = [tmp_path / f"notice-{index}.txt" for index in range(4)]
    processes = [
        ctx.Process(target=_claim_notice_in_child, args=(str(state_path), str(path), record.epoch))
        for path in paths
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sum(path.read_text(encoding="utf-8") == "true" for path in paths) == 1


def test_typed_health_preserves_fallback_walk(state_path, monkeypatch):
    from agent import auxiliary_client as aux

    aux._reset_aux_unhealthy_cache()
    aux._mark_provider_unhealthy("openrouter", ttl=30, reason="payment error")
    openrouter_client = object()
    nous_client = object()
    monkeypatch.setattr(
        aux, "_get_provider_chain",
        lambda: [("openrouter", lambda: (openrouter_client, "or-model")),
                 ("nous", lambda: (nous_client, "nous-model"))],
    )

    client, model, provider = aux._try_discovery_chain()

    assert (client, model, provider) == (nous_client, "nous-model", "nous")
    aux._reset_aux_unhealthy_cache()


def test_auxiliary_health_does_not_touch_main_primary_cooldown(state_path, tmp_path, monkeypatch):
    from agent import shared_primary_cooldown as primary

    primary_path = tmp_path / "state" / "model_cooldowns.json"
    monkeypatch.setattr(primary, "_state_path", lambda: primary_path)
    monkeypatch.setattr(primary, "_lock_path", lambda: primary_path.with_name(".model_cooldowns.lock"))
    route = ("openrouter", "", "main-model")
    primary.arm_cooldown(route, reason="primary outage", reset_at=time.time() + 60)

    health.mark_unhealthy(_identity(provider="openrouter"), ttl=30, reason="payment error")
    health.reset_health_state()

    assert primary.read_cooldown(route) is not None
    assert not state_path.exists()


def _claim_probe_in_child(state_path_text: str, result_path_text: str) -> None:
    from agent import auxiliary_health as child_health

    path = Path(state_path_text)
    child_health._state_path = lambda: path
    child_health._lock_path = lambda: path.with_name(".auxiliary_route_health.lock")
    result = child_health.claim_probe(_identity(), ttl=30)
    Path(result_path_text).write_text("true" if result is True else "false", encoding="utf-8")


def test_probe_claim_is_deduplicated_across_processes(state_path, tmp_path):
    identity = _identity()
    health.mark_unhealthy(identity, ttl=0, reason="rate limit")
    ctx = multiprocessing.get_context("spawn")
    paths = [tmp_path / f"probe-{index}.txt" for index in range(4)]
    processes = [
        ctx.Process(target=_claim_probe_in_child, args=(str(state_path), str(path)))
        for path in paths
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sum(path.read_text(encoding="utf-8") == "true" for path in paths) == 1


def _clear_probe_in_child(state_path_text: str, result_path_text: str) -> None:
    from agent import auxiliary_health as child_health

    path = Path(state_path_text)
    child_health._state_path = lambda: path
    child_health._lock_path = lambda: path.with_name(".auxiliary_route_health.lock")
    result = child_health.clear_health_after_probe(_identity())
    Path(result_path_text).write_text("true" if result else "false", encoding="utf-8")


def test_non_owner_cannot_clear_an_active_probe(state_path, tmp_path):
    identity = _identity()
    health.mark_unhealthy(identity, ttl=0, reason="rate limit")
    assert health.claim_probe(identity, ttl=30) is True

    result_path = tmp_path / "clear-probe.txt"
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_clear_probe_in_child, args=(str(state_path), str(result_path)))
    process.start()
    process.join(10)
    assert process.exitcode == 0
    assert result_path.read_text(encoding="utf-8") == "false"

    assert health.clear_health_after_probe(identity) is True


def test_expired_route_keeps_health_after_probe_claim_until_successful_clear(state_path):
    identity = _identity()
    health.mark_unhealthy(identity, ttl=0, reason="rate limit")

    assert health.claim_probe(identity, ttl=30) is True
    assert health.read_health(identity) is None
    assert health.claim_probe(identity, ttl=30) is False


def test_probe_owner_clears_after_outliving_its_claim(state_path, monkeypatch):
    identity = _identity()
    now = [100.0]
    monkeypatch.setattr(health.time, "time", lambda: now[0])
    health.mark_unhealthy(identity, ttl=0, reason="rate limit")
    assert health.claim_probe(identity, ttl=30) is True

    now[0] = 400.0

    assert health.clear_health_after_probe(identity) is True
    assert health.claim_probe(identity) is None


def test_default_probe_claim_outlasts_a_slow_auxiliary_call(state_path, monkeypatch):
    identity = _identity()
    now = [100.0]
    monkeypatch.setattr(health.time, "time", lambda: now[0])
    health.mark_unhealthy(identity, ttl=0, reason="rate limit")
    assert health.claim_probe(identity) is True

    now[0] = 100.0 + 180.0

    assert health.claim_probe(identity) is False


def test_failed_state_write_preserves_previous_health_record(state_path, monkeypatch):
    identity = _identity()
    health.mark_unhealthy(identity, ttl=30, reason="payment error")
    before = state_path.read_text(encoding="utf-8")
    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(health, "atomic_write_text", fail_write)

    with pytest.raises(OSError, match="disk full"):
        health.mark_unhealthy(identity, ttl=30, reason="rate limit")

    assert state_path.read_text(encoding="utf-8") == before
    assert health.read_health(identity).lane is health.AuxiliaryHealthLane.QUOTA


def test_auxiliary_health_store_is_independent_of_compression_state(state_path):
    compression_path = state_path.with_name("compression_failure_state.json")
    compression_path.parent.mkdir(parents=True, exist_ok=True)
    compression_path.write_text('{"cooldown_until": 123}', encoding="utf-8")

    health.mark_unhealthy(_identity(), ttl=30, reason="payment error")
    health.reset_health_state()

    assert compression_path.read_text(encoding="utf-8") == '{"cooldown_until": 123}'
    assert not state_path.exists()
