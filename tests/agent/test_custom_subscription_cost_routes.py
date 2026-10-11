"""Successful physical routes must reach every session-cost producer."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB

URL = "https://shared.invalid/v1"
MODEL = "billing-fixture"


def response():
    return SimpleNamespace(
        model=MODEL, id=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content="summary", tool_calls=[]), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=50, cost=0.25),
    )


@pytest.fixture
def billing_fixture(tmp_path, monkeypatch):
    from agent import auxiliary_client as aux
    from agent.aux_accounting import set_accounting_context, reset_accounting_context
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({
        "providers": {
            "included": {"base_url": URL, "api_key": "fixture-placeholder", "billing_mode": "subscription_included"},
            "metered": {"base_url": URL, "api_key": "fixture-placeholder"},
        },
        "auxiliary": {"transient_retries": 1, "transient_retry_base_seconds": 0},
    }), encoding="utf-8")
    token = set_hermes_home_override(home)
    aux._client_cache.clear()
    aux._aux_unhealthy_until.clear()
    db = SessionDB(db_path=home / "state.db")
    accounting_token = set_accounting_context(db, "audit")
    try:
        yield aux, db, home
    finally:
        reset_accounting_context(accounting_token)
        db.close()
        aux._client_cache.clear()
        aux._aux_unhealthy_until.clear()
        reset_hermes_home_override(token)


def assert_aux_row(db, task, provider="custom:included"):
    row = dict(db._conn.execute(
        "SELECT * FROM session_model_usage WHERE session_id = 'audit' AND task = ?", (task,)
    ).fetchone())
    included = provider == "custom:included"
    assert row["billing_provider"] == provider
    assert row["billing_base_url"].rstrip("/") == URL
    assert row["billing_mode"] == ("subscription_included" if included else "")
    assert row["estimated_cost_usd"] == (0 if included else 0.25)
    assert row["actual_cost_usd"] == 0  # Preserve the existing auxiliary display bucket.
    assert row["cost_status"] == ("included" if included else "actual")
    assert row["cost_source"] == ("none" if included else "provider_cost_api")


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("retry", [False, True])
def test_explicit_aux_route_keeps_named_identity_on_retry(billing_fixture, monkeypatch, async_call, retry):
    aux, db, _ = billing_fixture
    calls = []
    def physical(client, kwargs, *a, **kw):
        calls.append(str(client.base_url).rstrip("/"))
        if retry and len(calls) == 1:
            raise ConnectionError("connection reset by peer")
        return response()
    async def aphysical(*a, **kw):
        return physical(*a, **kw)
    monkeypatch.setattr(aux, "_create_with_progress", physical)
    monkeypatch.setattr(aux, "_acreate_with_progress", aphysical)
    # The routing and recording facades are real; only the physical SDK send is replaced.
    kwargs = dict(task="title_generation", provider="custom:included", model=MODEL,
                  messages=[{"role": "user", "content": "title"}])
    out = asyncio.run(aux.async_call_llm(**kwargs)) if async_call else aux.call_llm(**kwargs)
    assert out._hermes_billing_route["provider"] == "custom:included"
    assert calls == [URL] * (2 if retry else 1)
    assert_aux_row(db, "title_generation")


@pytest.mark.parametrize("async_call", [False, True])
def test_auto_aux_alias_switch_and_cache_isolation(billing_fixture, monkeypatch, async_call):
    aux, db, _ = billing_fixture
    monkeypatch.setattr(aux, "_create_with_progress", lambda *a, **k: response())
    async def physical(*a, **k):
        return response()
    monkeypatch.setattr(aux, "_acreate_with_progress", physical)
    for selected in ("custom:included", "custom:metered", "custom:included"):
        runtime = dict(provider="custom", requested_provider=selected, model=MODEL,
                       base_url=URL, api_key="fixture-placeholder", api_mode="chat_completions")
        kwargs = dict(task="title_generation", main_runtime=runtime, messages=[{"role": "user", "content": "title"}])
        if async_call:
            asyncio.run(aux.async_call_llm(**kwargs))
        else:
            aux.call_llm(**kwargs)
        assert_aux_row(db, "title_generation", selected)
        db._conn.execute("DELETE FROM session_model_usage")
        db._conn.commit()


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("starting, destination", [("metered", "included"), ("included", "metered")])
def test_aux_fallback_records_destination_not_requested_route(billing_fixture, monkeypatch, async_call, starting, destination):
    aux, db, home = billing_fixture
    config = json.loads((home / "config.yaml").read_text())
    config["providers"][starting]["base_url"] = "https://failed.invalid/v1"
    config["auxiliary"]["title_generation"] = {
        "provider": f"custom:{starting}", "model": MODEL,
        "fallback_chain": [{"provider": f"custom:{destination}", "model": MODEL}],
    }
    (home / "config.yaml").write_text(json.dumps(config))
    calls = []
    def physical(client, kwargs, *a, **kw):
        calls.append(client)
        if len(calls) == 1:
            raise RuntimeError("insufficient_quota")
        return response()
    async def aphysical(*a, **kw):
        return physical(*a, **kw)
    monkeypatch.setattr(aux, "_create_with_progress", physical)
    monkeypatch.setattr(aux, "_acreate_with_progress", aphysical)
    kwargs = dict(task="title_generation", messages=[{"role": "user", "content": "title"}])
    out = asyncio.run(aux.async_call_llm(**kwargs)) if async_call else aux.call_llm(**kwargs)
    assert len(calls) == 2
    assert out._hermes_billing_route["provider"] == f"custom:{destination}"
    assert_aux_row(db, "title_generation", f"custom:{destination}")


def test_compressor_inherits_live_selected_declaration(billing_fixture, monkeypatch):
    aux, db, _ = billing_fixture
    from run_agent import AIAgent
    monkeypatch.setattr(aux, "_create_with_progress", lambda *a, **k: response())
    agent = AIAgent(provider="custom", requested_provider="custom:included", model=MODEL,
                    base_url=URL, api_key="fixture-placeholder", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, save_trajectories=False,
                    enabled_toolsets=["file"])
    try:
        assert agent.context_compressor._call_summary_llm("summarize", time.monotonic()) == "summary"
        assert_aux_row(db, "compression")
        db._conn.execute("DELETE FROM session_model_usage")
        db._conn.commit()
        agent.requested_provider = "custom:metered"
        assert agent.context_compressor._call_summary_llm("summarize", time.monotonic()) == "summary"
        assert_aux_row(db, "compression", "custom:metered")
    finally:
        agent.close()


def test_background_review_preserves_selected_route_and_cost_status(billing_fixture):
    from agent.background_review import (
        _resolve_review_runtime, _fork_init_kwargs, _snapshot_review_usage, _record_review_usage_to_parent,
    )
    _, db, _ = billing_fixture
    fake_agent = SimpleNamespace(
        provider="custom", requested_provider="custom:included", model=MODEL, base_url=URL,
        api_key="fixture-placeholder", api_mode="chat_completions", max_iterations=2,
        session_input_tokens=1000, session_output_tokens=50, session_cache_read_tokens=0,
        session_cache_write_tokens=0, session_reasoning_tokens=0, session_api_calls=1,
        session_estimated_cost_usd=0, session_cost_status="included", session_cost_source="none",
    )
    fake_agent._current_main_runtime = lambda: vars(fake_agent).copy()
    fake_agent.platform, fake_agent.session_id = "cli", "audit"
    runtime = _resolve_review_runtime(fake_agent, {})
    kwargs = _fork_init_kwargs(fake_agent, runtime, False, 2, None)
    assert kwargs["requested_provider"] == "custom:included"
    usage = _snapshot_review_usage(fake_agent)
    assert usage["provider"] == "custom:included"
    _record_review_usage_to_parent(SimpleNamespace(_session_db=db, session_id="audit"), usage)
    assert_aux_row(db, "background_review")
    fake_agent.provider = "moa"
    fake_agent.model, fake_agent.base_url = "preset", "moa://local"
    fake_agent.client = SimpleNamespace(last_aggregator_billing_route={
        "provider": "custom:included", "model": MODEL, "base_url": URL})
    snapshot = _snapshot_review_usage(fake_agent)
    assert (snapshot["provider"], snapshot["model"], snapshot["base_url"]) == ("custom:included", MODEL, URL)


def test_custom_responses_usage_uses_selected_declaration(billing_fixture):
    from agent.codex_runtime import _record_codex_app_server_usage
    _, db, _ = billing_fixture
    fake_agent = SimpleNamespace(
        provider="custom", requested_provider="custom:included", model=MODEL, base_url=URL,
        api_key="fixture-placeholder", session_id="audit", _session_db=db, _session_db_created=True, session_api_calls=0,
        session_input_tokens=0, session_output_tokens=0, session_cache_read_tokens=0,
        session_cache_write_tokens=0, session_reasoning_tokens=0, session_estimated_cost_usd=0,
        session_cost_status="unknown", session_cost_source="none", _last_prompt_tokens=0,
        session_prompt_tokens=0, session_completion_tokens=0, session_total_tokens=0,
    )
    _record_codex_app_server_usage(fake_agent, SimpleNamespace(token_usage_last={"inputTokens": 1000, "outputTokens": 50}))
    db.flush_token_counts()
    row = dict(db._conn.execute("SELECT * FROM session_model_usage WHERE session_id = 'audit'").fetchone())
    assert row["billing_provider"] == "custom:included"
    assert row["billing_mode"] == "subscription_included"
    assert row["cost_status"] == "included"
