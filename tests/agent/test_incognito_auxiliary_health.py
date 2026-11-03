"""Private auxiliary failures retain fallback without touching shared route health."""
from unittest.mock import MagicMock

import pytest

from agent import auxiliary_client as aux
from agent import auxiliary_health as health
from agent.incognito import incognito_scope
from tests.agent.test_auxiliary_fallback_multihop import _client, _ok_response, _quota_429, _walk_patches


def _files(home):
    from hermes_logging import flush_log_queue
    flush_log_queue()
    return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}


@pytest.mark.parametrize("preexisting", ["empty", "public", "corrupt", "stale"])
def test_private_health_boundary_never_changes_files(tmp_path, monkeypatch, preexisting):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("time.time", lambda: 100_000.0)
    identity = aux._auxiliary_route_identity("openrouter")
    path = health._state_path()
    if preexisting in {"public", "stale"}:
        health.mark_unhealthy(identity, ttl=30 if preexisting == "public" else 0, reason="payment error")
    elif preexisting == "corrupt":
        path.parent.mkdir(parents=True)
        path.write_bytes(b"corrupt-public-state")
    before = _files(tmp_path)
    with incognito_scope():
        # This read used to create a lock, quarantine corrupt input or prune shared state.
        assert health.read_health(identity) is None
        assert _files(tmp_path) == before
        record = health.mark_unhealthy(identity, ttl=30, reason="private-model-canary")
        assert health.read_health(identity) == record
        assert health.claim_notice(identity, record.epoch)
        assert not health.claim_notice(identity, record.epoch)
        health.clear_health(identity)
        health.mark_unhealthy(identity, ttl=0, reason="rate limit")
        assert health.claim_probe(identity)
        assert not health.claim_probe(identity)
        assert health.clear_health_after_probe(identity)
        health.reset_health_state()
    assert _files(tmp_path) == before
    # The very same public API still owns durable health normally.
    public = health.mark_unhealthy(identity, ttl=30, reason="payment error")
    assert health.read_health(identity) == public
    assert path.exists()
    assert _files(tmp_path) != before or preexisting == "public"


def test_health_file_transaction_is_serialized_with_private_entry(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    from threading import Event
    from agent.incognito import is_incognito, register_session
    from agent.memory_provider import ctx_bound

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    identity = aux._auxiliary_route_identity("openrouter")
    entered, release = Event(), Event()
    state_path = health._state_path

    def held_state_path():
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return state_path()

    monkeypatch.setattr(health, "_state_path", held_state_path)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with incognito_scope(False, "health-transaction"):
                future = pool.submit(ctx_bound(health.read_health), identity)
            try:
                assert entered.wait(5)
                # Entering the boundary must wait until the admitted public
                # lock-file/read/repair/write transaction has finished.
                with ThreadPoolExecutor(max_workers=1) as boundary:
                    entering = boundary.submit(register_session, "health-transaction")
                    with pytest.raises(FuturesTimeout):
                        entering.result(timeout=0.3)
                    assert not is_incognito("health-transaction")
                    release.set()
                    assert future.result(timeout=5) is None
                    entering.result(timeout=5)
                assert is_incognito("health-transaction")
                register_session("health-transaction", False)
            finally:
                release.set()
        before = _files(tmp_path)
        register_session("health-transaction")
        with incognito_scope(False, "health-transaction"):
            record = health.mark_unhealthy(identity, ttl=30, reason="PRIVATE_ATOMIC_HEALTH_CANARY")
            assert record is not None and health.read_health(identity) == record
        assert _files(tmp_path) == before
        register_session("health-transaction", False)
        assert health.mark_unhealthy(identity, ttl=30, reason="payment error") is not None
        assert state_path().is_file()
    finally:
        release.set()
        register_session("health-transaction", False)


@pytest.mark.parametrize("prepopulated", [False, True])
def test_real_private_failure_walk_is_ram_only_and_does_not_poison_public(tmp_path, monkeypatch, prepopulated):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    aux._reset_aux_unhealthy_cache()
    if prepopulated:
        aux._mark_provider_unhealthy("nous", ttl=30, reason="payment error")
    before = _files(tmp_path)
    primary = _client("http://127.0.0.1:1/v1", MagicMock(side_effect=_quota_429("private-primary")))
    failed = _client("http://127.0.0.1:2/v1", MagicMock(side_effect=_quota_429("private-fallback")))
    success = _client("http://127.0.0.1:3/v1", MagicMock(return_value=_ok_response("private answer")))
    patches = _walk_patches(primary, [(failed, "private-model-canary", "custom"), (success, "ok", "custom")])
    with incognito_scope(), patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = aux.call_llm(task="title_generation", messages=[{"role": "user", "content": "private-content-canary"}])
        assert result.choices[0].message.content == "private answer"
        assert aux._is_provider_unhealthy("custom", "http://127.0.0.1:2/v1", "private-model-canary")
        assert not aux._is_provider_unhealthy("custom", "http://127.0.0.1:3/v1", "ok")
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        other_home = tmp_path / "other-profile"
        token = set_hermes_home_override(other_home)
        try:
            assert not aux._is_provider_unhealthy("custom", "http://127.0.0.1:2/v1", "private-model-canary")
            aux._mark_provider_unhealthy("anthropic", ttl=30, reason="payment error")
            assert aux._is_provider_unhealthy("anthropic")
        finally:
            reset_hermes_home_override(token)
        assert not aux._is_provider_unhealthy("anthropic")
        assert aux._is_provider_unhealthy("custom", "http://127.0.0.1:2/v1", "private-model-canary")
        assert not other_home.exists()
    assert failed.chat.completions.create.call_count == success.chat.completions.create.call_count == 1
    assert _files(tmp_path) == before
    assert not aux._is_provider_unhealthy("custom", "http://127.0.0.1:2/v1", "private-model-canary")
    assert aux._is_provider_unhealthy("nous") is prepopulated
    aux._mark_provider_unhealthy("openrouter", ttl=30, reason="payment error")
    assert health.read_health(aux._auxiliary_route_identity("openrouter")) is not None
    assert health._state_path().exists()
