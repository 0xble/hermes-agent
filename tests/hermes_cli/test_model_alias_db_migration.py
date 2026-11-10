"""The migration reaches authoritative topics without overwriting a newer routing row."""
import json
import sqlite3

import pytest

from hermes_cli.config_migrations_alias_routing import apply_routing_migration, plan_routing_migration
from hermes_cli.config_migrations_model_aliases import migrate_stores
from hermes_state import SessionDB


ALIASES = {"attended": {"model": "opus", "provider": "custom:claude"}}


def _seed(home, key="topic", *, scope="", model="@attended"):
    db = SessionDB(home / "state.db")
    try:
        db.save_gateway_routing_entry(key, json.dumps({
            "session_id": "original", "model_override": {"model": model, "provider": "stale"},
            "custom_counter": 7,
        }), scope=scope)
    finally:
        db.close()


def _rows(home):
    with sqlite3.connect(home / "state.db") as conn:
        return conn.execute("SELECT scope, session_key, entry_json, updated_at FROM gateway_routing ORDER BY session_key, scope").fetchall()


def test_dry_run_preserves_database_and_apply_migrates_every_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path)
    _seed(tmp_path, scope="second-profile")
    before = (tmp_path / "state.db").read_bytes()
    rows = _rows(tmp_path)
    assert migrate_stores(tmp_path, ALIASES, dry_run=True)
    assert (tmp_path / "state.db").read_bytes() == before
    assert _rows(tmp_path) == rows
    migrate_stores(tmp_path, ALIASES, dry_run=False)
    for scope, key, body, _ in _rows(tmp_path):
        assert json.loads(body) == {"session_id": "original", "model_override": None,
                                    "model_role": "attended", "custom_counter": 7}
    after = _rows(tmp_path)
    assert migrate_stores(tmp_path, ALIASES, dry_run=False) == []
    assert _rows(tmp_path) == after


def test_concurrent_topic_edit_refuses_and_rolls_back_other_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, "first")
    _seed(tmp_path, "second")
    pending = plan_routing_migration(tmp_path, ALIASES, [])
    _seed(tmp_path, "second", model="a-new-concrete-pick")
    before = _rows(tmp_path)
    with pytest.raises(ValueError, match="changed during migration"):
        apply_routing_migration(tmp_path, pending)
    assert _rows(tmp_path) == before


def test_unknown_database_alias_refuses_before_rewriting_json_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, model="@missing")
    path = tmp_path / "cron/jobs.json"
    path.parent.mkdir()
    path.write_text('{"jobs": [{"id": "one", "model": "@attended"}]}')
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unknown model alias"):
        migrate_stores(tmp_path, ALIASES, dry_run=False)
    assert path.read_bytes() == before
