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


def _seed_delivery(
    db, *, obligation_id="ob-1", session_key="agent:alpha:1", content="payload", adapter_profile="alpha"
):
    from gateway.delivery_ledger import _initialize_schema

    db._execute_write(_initialize_schema)
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id, content, state,
                attempts, created_at, updated_at, last_error, adapter_profile)
               VALUES (?, ?, 'telegram', 'chat-1', 'thread-1', ?, 'pending', 2,
                       1.0, 2.0, ?, ?)""",
            (obligation_id, session_key, content, "E" * 10000, adapter_profile),
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


def test_session_state_named_profile_uses_home_identity_without_bound_profile(monkeypatch, tmp_path):
    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    db = SessionDB(db_path=profile_home / "state.db")
    try:
        db.create_session("work-id", "telegram", profile_name="work")
    finally:
        db.close()

    discover_builtin_tools()
    result = _dispatch(registry, {"action": "inspect_session", "session_id": "work-id"})
    assert result["found"] is True
    assert result["session"]["profile_name"] == "work"


def test_session_state_multiplex_delivery_reads_process_home_ledger(monkeypatch, tmp_path):
    root = tmp_path / "root"
    secondary_home = root / "profiles" / "secondary"
    secondary_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    _set_profile(monkeypatch, "secondary")
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    profile_db = SessionDB(db_path=secondary_home / "state.db")
    process_db = SessionDB(db_path=root / "state.db")
    try:
        profile_db.create_session(
            "secondary-id", "telegram", profile_name="secondary", session_key="agent:secondary:1"
        )
        _seed_delivery(
            profile_db, obligation_id="profile-copy", session_key="agent:secondary:1", adapter_profile="secondary"
        )
        _seed_delivery(
            process_db, obligation_id="shared-ledger", session_key="agent:secondary:1", adapter_profile="secondary"
        )
        _seed_delivery(
            process_db, obligation_id="other-profile", session_key="agent:secondary:1", adapter_profile="default"
        )
    finally:
        profile_db.close()
        process_db.close()

    discover_builtin_tools()
    token = set_hermes_home_override(str(secondary_home))
    try:
        by_session = _dispatch(registry, {"action": "inspect_delivery", "session_id": "secondary-id"})
        by_key = _dispatch(registry, {"action": "inspect_delivery", "session_key": "agent:secondary:1"})
    finally:
        reset_hermes_home_override(token)
    for result in (by_session, by_key):
        assert result["found"] is True
        assert [row["obligation_id"] for row in result["rows"]] == ["shared-ledger"]


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


@pytest.mark.parametrize("archived,auto_archived", [(0, 0), (1, 0), (1, 1)])
@pytest.mark.parametrize("has_auto_archived", [False, True])
def test_session_state_exposes_archive_provenance(
    monkeypatch, tmp_path, archived, auto_archived, has_auto_archived
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "default")
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("archive-id", "telegram", profile_name="default")
        db._write_sql(
            "UPDATE sessions SET archived = ?, auto_archived = ? WHERE id = ?",
            (archived, auto_archived, "archive-id"),
        )
        if not has_auto_archived:
            db._write_sql("ALTER TABLE sessions DROP COLUMN auto_archived", ())
        discover_builtin_tools()
        result = _dispatch(registry, {"action": "inspect_session", "session_id": "archive-id"})
        assert result["session"]["archived"] is bool(archived)
        if has_auto_archived:
            assert result["session"]["auto_archived"] is bool(auto_archived)
        else:
            assert "auto_archived" not in result["session"]
            columns = {row[1] for row in db._read_all("PRAGMA table_info(sessions)", ())}
            assert "auto_archived" not in columns
    finally:
        db.close()


@pytest.mark.parametrize("legacy_profile", [None, "", "   "])
@pytest.mark.parametrize("store_profile", ["default", "work"])
def test_session_state_legacy_session_owner_is_its_store(
    monkeypatch, tmp_path, legacy_profile, store_profile
):
    home = tmp_path if store_profile == "default" else tmp_path / "profiles" / store_profile
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _set_profile(monkeypatch, store_profile)
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("legacy-id", "telegram", profile_name=store_profile, session_key="agent:legacy:1")
        db._write_sql("UPDATE sessions SET profile_name = ? WHERE id = ?", (legacy_profile, "legacy-id"))
        _seed_delivery(db, session_key="agent:legacy:1", adapter_profile=store_profile)
        discover_builtin_tools()
        result = _dispatch(registry, {"action": "inspect_session", "session_id": "legacy-id"})
        assert result["found"] is True
        assert result["session"]["profile_name"] == store_profile
        delivery = _dispatch(registry, {"action": "inspect_delivery", "session_id": "legacy-id"})
        assert delivery["found"] is True

        _set_profile(monkeypatch, "other" if store_profile == "default" else "default")
        hidden = _dispatch(registry, {"action": "inspect_session", "session_id": "legacy-id"})
        assert hidden["found"] is False
        hidden_delivery = _dispatch(registry, {"action": "inspect_delivery", "session_id": "legacy-id"})
        assert hidden_delivery["found"] is False
        stored = db.get_session("legacy-id")
        assert stored is not None and stored["profile_name"] == legacy_profile
    finally:
        db.close()


@pytest.mark.parametrize("legacy_profile", [None, "", "   "])
def test_session_state_legacy_delivery_is_default_only(monkeypatch, tmp_path, legacy_profile):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "default")
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        _seed_delivery(db, adapter_profile=legacy_profile)
        discover_builtin_tools()
        for selector in ({"obligation_id": "ob-1"}, {"session_key": "agent:alpha:1"}):
            result = _dispatch(registry, {"action": "inspect_delivery", **selector})
            assert result["found"] is True
            assert result["rows"][0]["obligation_id"] == "ob-1"
            _set_profile(monkeypatch, "alpha")
            hidden = _dispatch(registry, {"action": "inspect_delivery", **selector})
            assert hidden["found"] is False
            _set_profile(monkeypatch, "default")
        stored = db._read_one("SELECT adapter_profile FROM delivery_obligations WHERE obligation_id = ?", ("ob-1",))
        assert stored is not None and stored[0] == legacy_profile
    finally:
        db.close()


def test_session_state_delivery_clips_all_text_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "alpha")
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        _seed_delivery(db, content="文" * 10000)
        fields = (
            "obligation_id", "session_key", "platform", "chat_id", "thread_id", "state",
            "resume_marker_session_id", "resume_marker_token", "resume_marker_marked_at", "resume_turn_id",
        )
        db._write_sql(
            "UPDATE delivery_obligations SET " + ", ".join(f"{field} = ?" for field in fields),
            tuple("文" * 10000 for _ in fields),
        )
        discover_builtin_tools()
        result = _dispatch(registry, {
            "action": "inspect_delivery", "obligation_id": "文" * 10000, "include_content": True,
        })
        assert result["ok"] is True
        row = result["rows"][0]
        for field in fields:
            assert row[field] == "文" * 511 + "…"
        assert len(row["content"]) == 8192
        assert len(row["last_error"]) == 4096
        assert row["attempts"] == 2
        assert row["owner_pid"] is None
    finally:
        db.close()


def test_session_state_large_delivery_keeps_rows_under_byte_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_profile(monkeypatch, "alpha")
    from hermes_state import SessionDB
    from tools.registry import discover_builtin_tools, registry

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for index in range(12):
            _seed_delivery(db, obligation_id=f"ob-{index:02d}", content="文" * 10000)
        discover_builtin_tools()
        raw = registry.dispatch("session_state", {
            "action": "inspect_delivery", "session_key": "agent:alpha:1", "include_content": True,
        })
        assert isinstance(raw, str)
        result = _json_result(raw)
        assert result["ok"] is True
        assert result["truncated"] is True
        assert 0 < len(result["rows"]) < 5
        assert result["rows"][0]["obligation_id"] == "ob-11"
        assert len(raw.encode("utf-8")) <= 64 * 1024
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
