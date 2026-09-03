"""Continuable /side routing and persistence."""

import sqlite3

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.base import MessageEvent, MessageType, Platform, SessionSource
from gateway.run import GatewayRunner
from gateway.slash_commands import GatewaySlashCommandsMixin
from gateway.config import GatewayConfig
from gateway.session import SessionStore
from hermes_state import SessionDB


def _source(
    *,
    user_id: str = "12345",
    thread_id: str = "42",
    profile: str | None = None,
    scope_id: str | None = None,
    business_connection_id: str | None = None,
) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="67890",
        chat_type="dm",
        user_id=user_id,
        thread_id=thread_id,
        profile=profile,
        scope_id=scope_id,
        business_connection_id=business_connection_id,
    )


def _reply(*, user_id: str = "12345", thread_id: str = "42") -> MessageEvent:
    return MessageEvent(
        text="continue",
        message_type=MessageType.TEXT,
        source=_source(user_id=user_id, thread_id=thread_id),
        user_id=user_id,
        message_id="user-reply-2",
        reply_to_message_id="side-response-1",
    )


def test_side_message_binding_is_durable_and_exactly_scoped(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session(
        "side-1",
        "telegram",
        user_id="12345",
        chat_id="67890",
        chat_type="dm",
        thread_id="42",
        session_key="parent:side:side-1",
        model_config={"_side_root": "side-1"},
    )
    db.record_side_message_binding(
        platform="telegram",
        chat_id="67890",
        thread_id="42",
        user_id="12345",
        message_id="side-response-1",
        side_route_key="parent:side:side-1",
        side_root_session_id="side-1",
    )
    db.close()

    reopened = SessionDB(db_path=path)
    try:
        found = reopened.resolve_side_message_binding(
            platform="telegram",
            chat_id="67890",
            thread_id="42",
            user_id="12345",
            message_id="side-response-1",
        )
        assert found is not None
        assert found["side_route_key"] == "parent:side:side-1"
        assert found["side_root_session_id"] == "side-1"
        assert reopened.resolve_side_message_binding(
            platform="telegram",
            chat_id="67890",
            thread_id="42",
            user_id="other-user",
            message_id="side-response-1",
        ) is None
        assert reopened.resolve_side_message_binding(
            platform="telegram",
            chat_id="67890",
            thread_id="other-topic",
            user_id="12345",
            message_id="side-response-1",
        ) is None
    finally:
        reopened.close()


def test_side_message_bindings_do_not_collide_across_transport_scopes(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    for root, route in (("side-a", "route-a"), ("side-b", "route-b")):
        db.create_session(root, "slack", session_key=route)
    common = {
        "platform": "slack",
        "chat_id": "C123",
        "thread_id": "171.1",
        "user_id": "U123",
        "message_id": "reply-1",
        "profile": "shared",
    }
    db.record_side_message_binding(
        **common,
        scope_id="TEAM-A",
        side_route_key="route-a",
        side_root_session_id="side-a",
    )
    db.record_side_message_binding(
        **common,
        scope_id="TEAM-B",
        side_route_key="route-b",
        side_root_session_id="side-b",
    )

    first = db.resolve_side_message_binding(**common, scope_id="TEAM-A")
    second = db.resolve_side_message_binding(**common, scope_id="TEAM-B")
    assert first is not None and first["side_route_key"] == "route-a"
    assert second is not None and second["side_route_key"] == "route-b"
    assert db.resolve_side_message_binding(**common, scope_id="TEAM-C") is None
    db.close()


def test_legacy_side_message_binding_schema_migrates_without_losing_routes(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("side-legacy", "telegram", session_key="legacy-route")
    db.record_side_message_binding(
        platform="telegram",
        chat_id="chat",
        thread_id="thread",
        user_id="user",
        message_id="message",
        side_route_key="legacy-route",
        side_root_session_id="side-legacy",
    )
    db.close()

    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            DROP INDEX idx_side_message_bindings_route;
            ALTER TABLE side_message_bindings RENAME TO side_message_bindings_v31;
            CREATE TABLE side_message_bindings (
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL,
                side_route_key TEXT NOT NULL,
                side_root_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                PRIMARY KEY (platform, chat_id, thread_id, user_id, message_id)
            );
            INSERT INTO side_message_bindings
            SELECT platform, chat_id, thread_id, user_id, message_id,
                   side_route_key, side_root_session_id, created_at
              FROM side_message_bindings_v31;
            DROP TABLE side_message_bindings_v31;
            CREATE INDEX idx_side_message_bindings_route
                ON side_message_bindings(side_route_key);
            UPDATE schema_version SET version = 30;
            """
        )

    migrated = SessionDB(db_path=path)
    try:
        found = migrated.resolve_side_message_binding(
            platform="telegram",
            chat_id="chat",
            thread_id="thread",
            user_id="user",
            message_id="message",
        )
        assert found is not None and found["side_route_key"] == "legacy-route"
        with sqlite3.connect(path) as conn:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(side_message_bindings)"
                ).fetchall()
            }
        assert {"profile", "scope_id", "business_connection_id"} <= columns
    finally:
        migrated.close()


def test_side_route_closes_without_rebinding_parent(tmp_path):
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    db = SessionDB(tmp_path / "state.db")
    store._db = db
    store._loaded = True
    source = _source()
    parent = store.get_or_create_session(source)
    route = f"{parent.session_key}:side:child"
    db.create_session("child", "telegram", session_key=route)
    bound = store.bind_session_route(route, "child", source)
    assert bound is not None
    assert bound.session_id == "child"
    assert store.close_session_route(route) == "child"
    parent_after = store.lookup_by_session_key(parent.session_key)
    assert parent_after is not None
    assert parent_after.session_id == parent.session_id
    child = db.get_session("child")
    assert child is not None
    assert child["end_reason"] == "side_closed"
    db.close()


@pytest.mark.asyncio
async def test_slash_commands_resolve_the_pinned_side_entry():
    mixin = GatewaySlashCommandsMixin()
    side = SimpleNamespace(session_id="side-tip")
    get_or_create = AsyncMock()
    object.__setattr__(
        mixin,
        "async_session_store",
        SimpleNamespace(
            lookup_by_session_key=AsyncMock(return_value=side),
            get_or_create_session=get_or_create,
        ),
    )
    event = _reply()
    event.metadata.update({
        "gateway_session_key": "parent:side:root",
        "gateway_session_id": "side-tip",
        "gateway_explicit_session_route": True,
    })

    entry = await mixin._session_entry_for_event(event)

    assert entry is side
    assert mixin._session_key_for_event(event) == "parent:side:root"
    get_or_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_anchor_prepares_strict_normal_session_route():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_db = SimpleNamespace(
        resolve_side_message_binding=AsyncMock(
            return_value={
                "side_route_key": "parent:side:side-1",
                "side_root_session_id": "side-1",
            }
        )
    )
    entry = SimpleNamespace(session_id="side-tip-2")
    object.__setattr__(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=SimpleNamespace(),
            lookup_by_session_key=AsyncMock(return_value=entry),
        ),
    )
    runner.session_store = runner._async_session_store._store
    event = _reply()

    await runner._prepare_side_reply_route(event)

    resolver = runner._session_db.resolve_side_message_binding
    resolver.assert_awaited_once_with(
        platform="telegram",
        chat_id="67890",
        thread_id="42",
        user_id="12345",
        profile="",
        scope_id="",
        business_connection_id="",
        message_id="side-response-1",
    )
    assert event.metadata == {
        "gateway_session_key": "parent:side:side-1",
        "gateway_session_id": "side-tip-2",
        "gateway_session_strict": True,
        "gateway_explicit_session_route": True,
        "gateway_side_root_session_id": "side-1",
    }


@pytest.mark.asyncio
async def test_closed_side_reply_stays_off_the_parent_route():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_db = SimpleNamespace(
        resolve_side_message_binding=AsyncMock(
            return_value={"side_route_key": "route", "side_root_session_id": "root"}
        )
    )
    object.__setattr__(
        runner,
        "_async_session_store",
        SimpleNamespace(_store=SimpleNamespace(), lookup_by_session_key=AsyncMock(return_value=None)),
    )
    runner.session_store = runner._async_session_store._store
    event = _reply()
    await runner._prepare_side_reply_route(event)
    assert event.metadata["gateway_side_route_unavailable"] is True
    assert event.metadata["gateway_explicit_session_route"] is True
    assert event.metadata["gateway_session_strict"] is True
    assert event.metadata["gateway_session_key"] == "route"


@pytest.mark.asyncio
async def test_side_delivery_receipt_records_message_ids():
    runner = GatewayRunner.__new__(GatewayRunner)
    record = AsyncMock()
    runner._session_db = SimpleNamespace(record_side_message_binding=record)
    event = _reply()
    event.metadata.update({
        "gateway_session_key": "route",
        "gateway_explicit_session_route": True,
        "gateway_side_root_session_id": "root",
    })
    await runner._record_side_delivery(event, ["one", "two"])
    assert {call.kwargs["message_id"] for call in record.await_args_list} == {"one", "two"}
    assert all(call.kwargs["profile"] == "" for call in record.await_args_list)
    assert all(call.kwargs["scope_id"] == "" for call in record.await_args_list)
    assert all(
        call.kwargs["business_connection_id"] == ""
        for call in record.await_args_list
    )


@pytest.mark.asyncio
async def test_close_side_runs_session_finalize_boundary():
    runner = GatewayRunner.__new__(GatewayRunner)
    entry = SimpleNamespace(session_id="child")
    object.__setattr__(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=SimpleNamespace(),
            lookup_by_session_key=AsyncMock(return_value=entry),
            close_session_route=AsyncMock(return_value="child"),
        ),
    )
    runner.session_store = runner._async_session_store._store
    runner._interrupt_and_clear_session = AsyncMock()
    object.__setattr__(runner, "_agent_cache_lock", None)
    runner._evict_cached_agent = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner._finalize_session_off_loop = AsyncMock()

    assert await runner._close_side_route("route", _source()) is True

    runner._interrupt_and_clear_session.assert_awaited_once_with(
        "route",
        _source(),
        interrupt_reason="Side session closed",
        invalidation_reason="side_closed",
    )
    runner._finalize_session_off_loop.assert_awaited_once_with(
        session_id="child",
        platform="telegram",
        reason="side_closed",
        old_session_id="child",
        new_session_id=None,
    )


@pytest.mark.asyncio
async def test_resume_inside_side_cannot_rebind_another_session():
    mixin = GatewaySlashCommandsMixin()
    object.__setattr__(mixin, "_session_db", MagicMock())
    event = _reply()
    event.text = "/resume parent"
    event.metadata.update({
        "gateway_session_key": "parent:side:root",
        "gateway_session_id": "side-tip",
        "gateway_explicit_session_route": True,
    })

    result = await mixin._handle_resume_command(event)

    assert "unavailable inside a side" in result


def test_side_stream_final_is_wrapped_once():
    from gateway.side_notifications import side_response_parts
    from gateway.stream_consumer import GatewayStreamConsumer

    initial_text, final_suffix = side_response_parts(
        "side_20260831_004611_3f45e1"
    )
    consumer = GatewayStreamConsumer(
        adapter=SimpleNamespace(),
        chat_id="chat",
        initial_text=initial_text,
        final_suffix=final_suffix,
    )
    consumer.finish("answer")
    _, final_text = consumer._queue.get_nowait()
    assert final_text == (
        "↗️ Side `3f45e1`\nanswer\n*Reply here to continue*"
    )


def test_side_is_canonical_and_spawn_is_removed():
    from hermes_cli.commands import resolve_command

    side = resolve_command("side")
    assert side is not None
    assert "continuable" in side.description.lower()
    assert side.aliases == ()
    assert resolve_command("spawn") is None
