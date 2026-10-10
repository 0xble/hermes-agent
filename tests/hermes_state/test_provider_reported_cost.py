"""Provider-reported per-call cost is captured and persisted without changing how existing readers
combine the estimated and actual buckets (COALESCE(actual_cost_usd, estimated_cost_usd, 0)).

Every row must display its provider-billed amount exactly once. Rows written before this change
keep their base totals, including the explicit legacy dual-written shape (est=3, actual=3,
status actual) that an earlier candidate turned into $6.50.
"""
from types import SimpleNamespace

import pytest

from agent.insights import InsightsEngine
from agent.usage_pricing import normalize_usage
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _billed(db, session_id, amount, **extra):
    """The main-turn write the agent queues for a provider-billed call (agent/turn_usage.py)."""
    db.update_token_counts(
        session_id, input_tokens=10, output_tokens=5, model="m", billing_provider="openrouter",
        estimated_cost_usd=amount, cost_status="actual", cost_source="provider_cost_api",
        api_call_count=1, **extra,
    )


def _estimated(db, session_id, amount):
    db.update_token_counts(
        session_id, input_tokens=10, output_tokens=5, model="m", billing_provider="openrouter",
        estimated_cost_usd=amount, cost_status="estimated", cost_source="official_docs_snapshot",
        api_call_count=1,
    )


def _displayed(db, session_id):
    """Every shipped reader of a session's spend: the store total, the session-filter SQL,
    the Desktop/project fallback, and Insights' overview and per-model totals."""
    db.flush_token_counts()
    row = dict(db._conn.execute(
        "SELECT estimated_cost_usd, actual_cost_usd, cost_status,"
        " COALESCE(actual_cost_usd, estimated_cost_usd, 0) AS shown FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone())
    report = InsightsEngine(db).generate(days=30)
    return {
        **row,
        "usage_totals": db.usage_totals(min_message_count=0)["cost_usd"],
        "or_fallback": float(row["actual_cost_usd"] or row["estimated_cost_usd"] or 0),
        "insights_models": sum(m["cost"] for m in report["models"]),
        "insights_model_actual": sum(m["actual_cost"] for m in report["models"]),
    }


def _model_row(db, session_id):
    return dict(db._conn.execute(
        "SELECT estimated_cost_usd, actual_cost_usd, cost_status, cost_source"
        " FROM session_model_usage WHERE session_id = ? AND task = ''", (session_id,),
    ).fetchone())


def test_billed_call_persists_amount_once_and_marks_it_actual(db):
    db.create_session(session_id="s", source="cli", model="m")
    _billed(db, "s", 0.03)
    _billed(db, "s", 0.02)

    shown = _displayed(db, "s")
    assert shown["cost_status"] == "actual"
    assert shown["shown"] == pytest.approx(0.05)
    assert shown["usage_totals"] == pytest.approx(0.05)
    assert shown["or_fallback"] == pytest.approx(0.05)
    assert shown["insights_models"] == pytest.approx(0.05)
    model = _model_row(db, "s")
    # The per-call model row records the provider amount as billed, in both buckets it reads.
    assert model["actual_cost_usd"] == pytest.approx(0.05)
    assert model["estimated_cost_usd"] == pytest.approx(0.05)
    assert (model["cost_status"], model["cost_source"]) == ("actual", "provider_cost_api")
    assert shown["insights_model_actual"] == pytest.approx(0.05)
    # The sessions row keeps one display bucket, so COALESCE readers cannot add it twice.
    assert shown["actual_cost_usd"] is None


def test_billed_then_estimated_call_is_not_double_counted(db):
    db.create_session(session_id="s", source="cli", model="m")
    _billed(db, "s", 0.03)
    _estimated(db, "s", 0.50)

    shown = _displayed(db, "s")
    for reader in ("shown", "usage_totals", "or_fallback", "insights_models"):
        assert shown[reader] == pytest.approx(0.53), reader


def test_legacy_dual_written_actual_row_then_estimated_call_shows_3_50(db):
    """Opposite-class case from the fourth P1: est=3, actual=3, status actual (displayed $3), then a
    genuinely estimated $0.50 call. Base behaviour is preserved: $3.00 is shown, never $6.50."""
    db.create_session(session_id="legacy", source="cli", model="m")
    db.update_token_counts(
        "legacy", input_tokens=10, model="m", billing_provider="openrouter",
        estimated_cost_usd=3.0, actual_cost_usd=3.0, cost_status="actual", cost_source="provider_cost_api",
        api_call_count=1,
    )
    before = _displayed(db, "legacy")
    _estimated(db, "legacy", 0.50)
    after = _displayed(db, "legacy")

    assert before["shown"] == pytest.approx(3.0)
    # Unchanged base semantics for an estimate on a legacy row: the actual bucket keeps winning.
    assert after["shown"] == pytest.approx(3.0)
    for reader in ("shown", "usage_totals", "or_fallback"):
        assert after[reader] != pytest.approx(6.5), reader


def test_legacy_dual_written_actual_row_then_billed_call_adds_the_billed_amount_once(db):
    db.create_session(session_id="legacy", source="cli", model="m")
    db.update_token_counts(
        "legacy", input_tokens=10, model="m", billing_provider="openrouter",
        estimated_cost_usd=3.0, actual_cost_usd=3.0, cost_status="actual", cost_source="provider_cost_api",
        api_call_count=1,
    )
    _billed(db, "legacy", 0.25)

    shown = _displayed(db, "legacy")
    for reader in ("shown", "usage_totals", "or_fallback"):
        assert shown[reader] == pytest.approx(3.25), reader


def test_rows_without_a_provider_amount_keep_base_columns(db):
    db.create_session(session_id="e", source="cli", model="m")
    _estimated(db, "e", 0.50)
    db.update_token_counts("e", input_tokens=10, model="m", estimated_cost_usd=1.25, actual_cost_usd=1.0,
                           cost_status="estimated", cost_source="provider", api_call_count=1)

    db.flush_token_counts()
    row = dict(db._conn.execute(
        "SELECT estimated_cost_usd, actual_cost_usd, cost_status FROM sessions WHERE id = 'e'").fetchone())
    assert row == {"estimated_cost_usd": pytest.approx(1.75), "actual_cost_usd": pytest.approx(1.0),
                   "cost_status": "estimated"}


def test_queued_billed_deltas_coalesce_to_the_same_totals(db):
    db.create_session(session_id="q", source="cli", model="m")
    for amount in (0.01, 0.02, 0.03):
        db.queue_token_counts("q", input_tokens=10, model="m", billing_provider="openrouter",
                              estimated_cost_usd=amount, cost_status="actual",
                              cost_source="provider_cost_api", api_call_count=1)
    shown = _displayed(db, "q")
    assert shown["shown"] == pytest.approx(0.06)
    assert _model_row(db, "q")["actual_cost_usd"] == pytest.approx(0.06)


def test_main_turn_persists_provider_reported_cost(tmp_path, monkeypatch):
    """End to end through record_response_usage: an OpenRouter-style plain-object usage with a flat
    ``cost`` lands in SessionDB as the billed amount, displayed once."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from agent import turn_usage
    from run_agent import AIAgent

    session_db = SessionDB(db_path=tmp_path / "turn.db")
    agent = AIAgent(api_key="k", base_url="https://openrouter.ai/api/v1", provider="openrouter",
                    api_mode="chat_completions", model="openai/gpt-5.4-mini", session_id="t", platform="cli",
                    session_db=session_db, quiet_mode=True, skip_context_files=True, skip_memory=True,
                    save_trajectories=False, enabled_toolsets=["file"])
    try:
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=50, total_tokens=1050, cost=0.0123,
                                prompt_tokens_details=None, completion_tokens_details=None)
        turn_usage.record_response_usage(
            agent, SimpleNamespace(usage=usage, model="openai/gpt-5.4-mini", id=None),
            messages=[{"role": "user", "content": "hi"}], api_call_count=1, api_duration=0.1,
            compression_attempts=0, max_compression_attempts=3)
        shown = _displayed(session_db, "t")
        assert shown["cost_status"] == "actual"
        assert shown["shown"] == pytest.approx(0.0123)
        assert _model_row(session_db, "t")["actual_cost_usd"] == pytest.approx(0.0123)
    finally:
        agent.close()
        session_db.close()


def test_plain_object_usage_keeps_provider_cost_for_pricing():
    usage = normalize_usage(SimpleNamespace(prompt_tokens=10, completion_tokens=2, cost=0.004))
    assert usage.raw_usage["cost"] == 0.004
