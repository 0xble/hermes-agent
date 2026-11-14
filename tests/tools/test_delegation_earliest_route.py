"""Durable delegation retries wait for the earliest recovering route, not the last fallback's reset."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent import shared_primary_cooldown as spc
from tools import async_delegation as ad
from tools import delegation_resume as dr

PRIMARY = ("openai", "https://primary.invalid/v1", "gpt-primary")
FALLBACK = ("muse", "https://muse.invalid/v1", "muse-last")
CHAIN = [list(PRIMARY), list(FALLBACK)]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _store(*entries):
    """Write the shared cooldown store directly (arm_cooldown ignores past resets)."""
    now = time.time()
    routes = {}
    for route, reset_at in entries:
        routes[spc.route_key(provider=route[0], base_url=route[1], model=route[2])] = {
            **spc._route_fields(route), "reset_at": reset_at, "reason": "rate_limit", "source": "provider_reset",
            "backoff_count": 1, "notice_claimed": False, "outage_id": "o-" + route[2], "recorded_at": now - 60}
    path = spc._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": spc._STATE_VERSION, "routes": routes}))


def _failed_delegation(did, now, *, hint="usage window resets in 48h", reason="rate_limit", chain=CHAIN):
    record = {
        "delegation_id": did, "goal": "g", "goals": ["g"], "is_batch": True, "context": "c", "toolsets": None,
        "role": "leaf", "model": "m", "session_key": "agent:main:telegram:dm:1", "origin_ui_session_id": "",
        "origin_session_id": "", "parent_session_id": "sess-owner", "dispatched_at": now - 60,
        **({"route_chain": chain} if chain else {})}
    ad._persist_dispatch(record)
    result = {"results": [{"task_index": 0, "status": "failed", "failure_reason": reason,
                           "error": f"HTTP 429: {hint}", "summary": hint}], "error": None}
    event = {"type": "async_delegation", "delegation_id": did, "status": "error", "error": None}
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='error', completed_at=?, updated_at=?, event_json=?, "
                     "result_json=?, delivery_state='delivered' WHERE delegation_id=?",
                     (now, now, json.dumps(event), json.dumps(result), did))
    return did


def test_retry_follows_earliest_route_not_last_fallback():
    now = time.time()
    # Production records only the primary when fallback_cooldown leaves it.
    _store((PRIMARY, now + 600))
    did = _failed_delegation("deleg_early", now)
    plan = dr.schedule_retry(did, now=now)
    assert plan["retry_state"] == "scheduled"
    assert now + 600 <= plan["due_at"] < now + 600 + dr.BASE_BACKOFF_S + 1


def test_primary_record_beats_a_long_provider_hint():
    now = time.time()
    _store((PRIMARY, now + 10 * 3600))
    did = _failed_delegation("deleg_primary_hint", now, hint="usage window resets in 3h")
    plan = dr.schedule_retry(did, now=now)
    assert now + 3 * 3600 <= plan["due_at"] < now + 3 * 3600 + dr.BASE_BACKOFF_S + 1


def test_all_routes_ready_uses_minimal_backoff():
    now = time.time()
    _store((PRIMARY, now - 10))
    did = _failed_delegation("deleg_ready", now)
    plan = dr.schedule_retry(did, now=now)
    assert plan["due_at"] == pytest.approx(now + dr.BASE_BACKOFF_S)


def test_no_store_data_falls_back_to_error_hint():
    now = time.time()
    did = _failed_delegation("deleg_hint", now, hint="Limit resets in 26m.")
    plan = dr.schedule_retry(did, now=now)
    assert plan["due_at"] >= now + 26 * 60


def test_unreadable_store_is_unknown_not_never():
    now = time.time()
    path = spc._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert spc.earliest_available([PRIMARY, FALLBACK], now=now) == (None, None)
    did = _failed_delegation("deleg_bad_store", now, hint="Limit resets in 26m.")
    assert dr.schedule_retry(did, now=now)["due_at"] >= now + 26 * 60


def test_earliest_available_reports_index():
    now = time.time()
    _store((PRIMARY, now + 600))
    assert spc.earliest_available([FALLBACK, PRIMARY], now=now) == (1, now + 600)
    assert spc.earliest_available([], now=now) == (None, None)


def test_non_transient_failure_without_cooldown_is_terminal():
    now = time.time()
    did = _failed_delegation("deleg_auth", now, hint="no auth available", reason="auth")
    assert dr.schedule_retry(did, now=now)["retry_state"] == "terminal"


def test_exhausted_copy_does_not_tell_user_to_wait_out_a_far_reset_when_primary_record_is_sooner():
    from agent.turn_failure_copy import exhausted_copy
    now = time.time()
    _store((PRIMARY, now + 600))
    agent = SimpleNamespace(_primary_runtime={"provider": PRIMARY[0], "base_url": PRIMARY[1], "model": PRIMARY[2]},
                            _fallback_chain=[{"provider": FALLBACK[0], "base_url": FALLBACK[1], "model": FALLBACK[2]}])
    earliest = spc.earliest_route_delay(agent, now=now)
    text = exhausted_copy("rate_limit", label="Muse", attempts=3, summary="429",
                          reset_seconds=48 * 3600, earliest_route_seconds=earliest)
    assert "earlier recorded reset" in text and "available now" not in text
    plain = exhausted_copy("rate_limit", label="Muse", attempts=3, summary="429")
    assert "available now" not in plain
