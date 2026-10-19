import json

import pytest


def _json_result(value):
    return json.loads(value) if isinstance(value, str) else value


def _dispatch(registry, args):
    return _json_result(registry.dispatch("session_state", args))


@pytest.fixture(autouse=True)
def _reset_session_context():
    from gateway.session_context import reset_session_vars

    reset_session_vars()
    yield
    reset_session_vars()


def _set_profile(monkeypatch, profile):
    monkeypatch.setenv("HERMES_SESSION_PROFILE", profile)
    from gateway.session_context import set_session_vars

    set_session_vars(profile=profile)


def _seed_delivery(db, *, obligation_id="ob-1", session_key="agent:alpha:1", content="payload"):
    from gateway.delivery_ledger import _initialize_schema

    db._execute_write(_initialize_schema)
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id, content, state,
                attempts, created_at, updated_at, last_error, adapter_profile)
               VALUES (?, ?, 'telegram', 'chat-1', 'thread-1', ?, 'pending', 2,
                       1.0, 2.0, ?, 'alpha')""",
            (obligation_id, session_key, content, "E" * 10000),
        )
    )


def test_session_state_is_telegram_bundle_only():
    from toolsets import TOOLSETS, resolve_toolset

    assert "session_state" in TOOLSETS["session_state"]["tools"]
    assert "session_state" in resolve_toolset("hermes-telegram")
    assert "session_state" not in resolve_toolset("hermes-cli")
    assert "session_state" not in resolve_toolset("hermes-acp")


def test_session_state_dispatch_reads_real_temp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "alpha")
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("same-id", "telegram", profile_name="alpha", session_key="agent:alpha:1")
        db._write_sql("UPDATE sessions SET title = ? WHERE id = ?", ("Alpha", "same-id"))
        db.append_message("same-id", "user", "secret transcript body")
    finally:
        db.close()

    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    result = _dispatch(registry, {"action": "inspect_session", "session_id": "same-id"})
    assert result["ok"] is True
    assert result["session"]["profile_name"] == "alpha"
    assert result["session"]["title"] == "Alpha"
    assert "messages" not in result["session"]
    assert "secret transcript body" not in json.dumps(result)

    _set_profile(monkeypatch, "beta")
    hidden = _dispatch(registry, {"action": "inspect_session", "session_id": "same-id"})
    assert hidden["found"] is False


def test_session_state_two_profiles_can_read_same_id_without_cross_profile_fallback(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    homes = {profile: tmp_path / profile for profile in ("alpha", "beta")}
    for profile, home in homes.items():
        db = SessionDB(db_path=home / "state.db")
        try:
            db.create_session("same-id", "telegram", profile_name=profile)
            db._write_sql("UPDATE sessions SET title = ? WHERE id = ?", (profile.title(), "same-id"))
        finally:
            db.close()

    discover_builtin_tools()
    for profile, home in homes.items():
        monkeypatch.setenv("HERMES_HOME", str(home))
        _set_profile(monkeypatch, profile)
        result = _dispatch(registry, {"action": "inspect_session", "session_id": "same-id"})
        assert result["session"]["title"] == profile.title()


def test_session_state_absent_db_does_not_create_one(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "alpha")
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    result = _dispatch(registry, {"action": "inspect_delivery"})
    assert result == {"ok": True, "action": "inspect_delivery", "found": False, "reason": "state_db_absent"}
    assert not (tmp_path / "state.db").exists()


def test_session_state_resolves_topic_and_delivery_through_real_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "alpha")
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("same-id", "telegram", profile_name="alpha", session_key="agent:alpha:1")
        db.apply_telegram_topic_migration()
        db.bind_telegram_topic(
            chat_id="chat-1", thread_id="thread-1", user_id="user-1", session_key="agent:alpha:1",
            session_id="same-id", profile_name="alpha",
        )
        _seed_delivery(db)
        before_row = db._read_one("PRAGMA journal_mode", ())
        before_version_row = db._read_one("PRAGMA user_version", ())
        assert before_row is not None and before_version_row is not None
        before = before_row[0]
        before_user_version = before_version_row[0]

        discover_builtin_tools()
        topic = _dispatch(registry, {"action": "resolve_topic", "chat_id": "chat-1", "thread_id": "thread-1"})
        assert topic["found"] is True
        assert topic["binding"]["session_id"] == "same-id"

        delivery = _dispatch(registry, {"action": "inspect_delivery", "session_id": "same-id"})
        assert delivery["found"] is True
        assert delivery["rows"][0]["state"] == "pending"
        assert "content" not in delivery["rows"][0]

        capped = _dispatch(registry, {"action": "inspect_delivery", "obligation_id": "ob-1", "include_content": True})
        assert len(capped["rows"][0]["content"]) <= 8192
        assert len(capped["rows"][0]["last_error"]) <= 4096
        assert len(json.dumps(capped).encode()) <= 64 * 1024
        after_row = db._read_one("PRAGMA journal_mode", ())
        after_version_row = db._read_one("PRAGMA user_version", ())
        assert after_row is not None and after_version_row is not None
        after = after_row[0]
        after_user_version = after_version_row[0]
        assert after == before
        assert after_user_version == before_user_version
    finally:
        db.close()


def test_session_state_rejects_sql_shaped_args(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.session_state_tool import session_state_tool

    result = session_state_tool({"action": "inspect_session", "session_id": "x OR 1=1"})
    assert "SQL-shaped selectors" in str(result)


def test_session_state_optional_tables_are_structured_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "alpha")
    from hermes_state import SessionDB
    from tools.session_state_tool import session_state_tool

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        result = _json_result(session_state_tool({"action": "resolve_topic", "chat_id": "1", "thread_id": "2"}))
        assert result["found"] is False
        assert result["reason"] == "optional_table_absent"
    finally:
        db.close()


def test_session_state_legacy_optional_table_is_not_migrated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "alpha")
    from hermes_state import SessionDB
    from tools.session_state_tool import session_state_tool

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db._execute_write(
            lambda conn: conn.execute(
                "CREATE TABLE telegram_dm_topic_bindings (chat_id TEXT, thread_id TEXT)"
            )
        )
        version_row = db._read_one("PRAGMA user_version", ())
        assert version_row is not None
        version = version_row[0]
        result = _json_result(session_state_tool({"action": "resolve_topic", "chat_id": "1", "thread_id": "2"}))
        assert result["reason"] == "optional_table_unsupported"
        columns = {row[1] for row in db._read_all("PRAGMA table_info(telegram_dm_topic_bindings)", ())}
        assert columns == {"chat_id", "thread_id"}
        after_row = db._read_one("PRAGMA user_version", ())
        assert after_row is not None and after_row[0] == version
    finally:
        db.close()


def test_session_state_errors_are_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB
    from tools.session_state_tool import session_state_tool

    db = SessionDB(db_path=tmp_path / "state.db")
    db.close()
    result = session_state_tool({"action": "inspect_delivery", "obligation_id": "a", "session_key": "b"})
    assert "exactly one" in result
    assert len(result.encode()) < 4096
