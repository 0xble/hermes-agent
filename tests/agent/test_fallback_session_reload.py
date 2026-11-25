"""A provider fallback stays turn-scoped across a session reload (#152333).

The fallback's model reached the durable session row (the first accounted API call, the gateway's
per-turn sync, the TUI's runtime persist), and every resume path rebuilt the session from that row,
so a reloaded session kept billing the fallback after the primary recovered. Readers now restore
the route the session ran before the fallback, while an explicit ``/model`` pick still survives.
"""

import json
from types import SimpleNamespace

from agent.fallback_session_route import FALLBACK_ROUTE_KEY, fallback_restored_row, mark_fallback_route
from hermes_state import SessionDB

PRIMARY = {"model": "glm-5.3", "provider": "zai", "base_url": "https://api.z.ai/api/paas/v4", "api_mode": "chat_completions"}


def _fallback_turn(db, sid="s1"):
    """The writes a real fallback turn makes: the marker at activation, then the first accounted call."""
    db.create_session(session_id=sid, source="tui", model="glm-5.3")
    agent = SimpleNamespace(session_id=sid, _session_db=db, _persist_disabled=False,
                            _primary_runtime={**PRIMARY, "requested_provider": "zai"})
    mark_fallback_route(agent, "deepseek-flash")
    db.update_token_counts(sid, input_tokens=10, output_tokens=2, model="deepseek-flash",
                           billing_provider="deepseek", billing_base_url="https://api.deepseek.com/v1", api_call_count=1)
    db.flush_token_counts()


def test_reload_after_a_fallback_turn_restores_the_primary_route(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        _fallback_turn(db)
        row = db.get_session("s1")
        assert row["model"] == "deepseek-flash"  # the row still records what was billed
        restored = fallback_restored_row(row)
        assert restored["model"] == "glm-5.3"
        assert SessionDB.session_gateway_runtime(restored) == {
            "provider": "zai", "base_url": PRIMARY["base_url"], "api_mode": "chat_completions"}

        from hermes_cli.cli_model_switch_mixin import stored_session_route
        from tui_gateway.server import _stored_session_runtime_overrides  # bound with the facade's globals
        route = stored_session_route(row, current_model="glm-5.3", current_provider="zai")
        assert route is None or route[0] == "glm-5.3"  # CLI --resume stays on (or returns to) the primary
        override = _stored_session_runtime_overrides(row).get("model_override") or {}
        assert override.get("model") == "glm-5.3" and override.get("provider") == "zai"
    finally:
        db.close()


def test_fallback_marker_is_best_effort_for_malformed_primary_runtime():
    patcher = SimpleNamespace(patch_session_model_config=lambda *args: (_ for _ in ()).throw(AssertionError("not reached")))
    agent = SimpleNamespace(session_id="s1", _session_db=patcher, _persist_disabled=False, _primary_runtime="stale")
    mark_fallback_route(agent, "deepseek-flash")


def test_an_explicit_model_pick_of_the_fallback_model_survives_reload(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        _fallback_turn(db)
        db.update_session_model("s1", "deepseek-flash", provider="deepseek", base_url="https://api.deepseek.com/v1")
        row = db.get_session("s1")
        assert FALLBACK_ROUTE_KEY not in json.loads(row["model_config"])
        assert fallback_restored_row(row) is row
        from tui_gateway.server import _stored_session_runtime_overrides  # bound with the facade's globals
        override = _stored_session_runtime_overrides(row)["model_override"]
        assert (override["model"], override["provider"]) == ("deepseek-flash", "deepseek")
    finally:
        db.close()


def test_legacy_gateway_fallback_rows_return_to_the_configured_model():
    """Rows synced before the marker existed carry only ``gateway_runtime.fallback_active``."""
    row = {"id": "s1", "model": "deepseek-flash", "billing_provider": "deepseek", "model_config": json.dumps({
        "gateway_runtime": {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "fallback_active": True},
        "reasoning_config": {"effort": "high"}})}
    restored = fallback_restored_row(row)
    assert restored["model"] is None and restored["billing_provider"] is None
    assert json.loads(restored["model_config"]) == {"reasoning_config": {"effort": "high"}}
    plain = {"model": "deepseek-flash", "model_config": json.dumps({"gateway_runtime": {"provider": "deepseek"}})}
    assert fallback_restored_row(plain) is plain


def _gateway_synced_fallback_row(db, sid="s1"):
    """A gateway session whose last turn ran on a fallback: the per-turn sync wrote the fallback's
    model with ``gateway_runtime.fallback_active`` (and the activation wrote the marker)."""
    _fallback_turn(db, sid)
    config = json.loads(db.get_session(sid)["model_config"])
    config["gateway_runtime"] = {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                                 "fallback_active": True}
    db.update_session_meta(sid, json.dumps(config), model="deepseek-flash")


def _resumed_model(row):
    return (fallback_restored_row(row) or {}).get("model")


def test_a_pick_on_a_gateway_synced_fallback_row_survives_reload(tmp_path):
    """Clearing only the marker left ``fallback_active``, so resume restored the profile default
    (model None) over the user's pick. Every explicit-pick writer clears both."""
    db = SessionDB(tmp_path / "state.db")
    try:
        _gateway_synced_fallback_row(db, "provider-less")
        db.update_session_model("provider-less", "user-selected")  # a /model pick without route knowledge
        assert _resumed_model(db.get_session("provider-less")) == "user-selected"

        _gateway_synced_fallback_row(db, "with-provider")
        db.update_session_model("with-provider", "user-selected", provider="zai")
        assert _resumed_model(db.get_session("with-provider")) == "user-selected"
    finally:
        db.close()


def test_a_local_policy_model_selection_on_a_gateway_synced_fallback_row_survives_reload(tmp_path, monkeypatch):
    import hermes_state_mutation_prepared as prepared_mod
    db = SessionDB(tmp_path / "state.db")
    try:
        _gateway_synced_fallback_row(db)
        # The policy receipt half of the transaction is not under test: only the row it writes.
        monkeypatch.setattr(prepared_mod, "_commit_policy", lambda db, conn, sid, prepared: ({"target": sid}, 1))
        prepared = {"policy": {"model": "user-selected", "config_json": json.dumps({"model": {"provider": "zai"}})}}
        db._execute_write(lambda conn: prepared_mod.model_in_transaction(db, conn, "s1", {}, prepared))
        row = db.get_session("s1")
        assert row["model"] == "user-selected"
        assert _resumed_model(row) == "user-selected"
    finally:
        db.close()
