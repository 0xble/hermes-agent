"""Generation-owned routing preserves obligations until the owner is truly idle."""
import asyncio
import json
import os
import sqlite3
import threading
import time
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.session_identity import RoutingIdentity

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.owned_routing import OwnedRouting, _owned_callback_replay, _restore_event
from gateway.status import _get_process_start_time


def _identity(release_sha, label):
    return GenerationIdentity.create(release_sha=release_sha, label=label,
        start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")


def _pending_delegation(home, *, owner_pid, owner_started_at):
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("""CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            delivery_state TEXT NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER
        )""")
        conn.execute(
            "INSERT INTO async_delegations VALUES (?, ?, 'pending', ?, ?)",
            ("delegation-1", "agent:default:telegram:chat-1", owner_pid, owner_started_at),
        )
        conn.commit()
    finally:
        conn.close()


def _routing_for_delegation(tmp_path, pid):
    coordinator = SimpleNamespace(home=tmp_path)
    generation = SimpleNamespace(
        coordinator=coordinator,
        identity=SimpleNamespace(pid=pid),
        runner=SimpleNamespace(adapters={}),
    )
    return OwnedRouting(generation)


def test_delegation_with_unknown_start_time_is_retained(tmp_path, monkeypatch):
    pid = os.getpid()
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda value: 1000)
    _pending_delegation(tmp_path, owner_pid=pid, owner_started_at=None)
    assert _routing_for_delegation(tmp_path, pid)._delegation_keys() == {
        "agent:default:telegram:chat-1"
    }


def test_delegation_with_drifting_start_time_is_retained(tmp_path, monkeypatch):
    pid = os.getpid()
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda value: 1000)
    _pending_delegation(tmp_path, owner_pid=pid, owner_started_at=1199)
    assert _routing_for_delegation(tmp_path, pid)._delegation_keys() == {
        "agent:default:telegram:chat-1"
    }


def test_delegation_is_retained_when_process_start_time_is_unavailable(tmp_path, monkeypatch):
    pid = os.getpid()
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda value: None)
    _pending_delegation(tmp_path, owner_pid=pid, owner_started_at=1000)
    assert _routing_for_delegation(tmp_path, pid)._delegation_keys() == {
        "agent:default:telegram:chat-1"
    }


def test_delegation_with_reused_pid_is_excluded(tmp_path, monkeypatch):
    pid = os.getpid()
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda value: 1000)
    _pending_delegation(tmp_path, owner_pid=pid, owner_started_at=2001)
    assert _routing_for_delegation(tmp_path, pid)._delegation_keys() == set()


@pytest.mark.asyncio
async def test_native_restore_replays_local_event_but_deduplicates_new_transport_object(tmp_path):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter
    from plugins.platforms.telegram.adapter import TelegramAdapter
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "owner")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    runner = SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch, runner=runner))
    adapter = object.__new__(TelegramAdapter)
    BasePlatformAdapter.__init__(adapter, PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.gateway_runner = runner
    adapter._owned_routing = routing
    adapter._is_sender_authorized = lambda *a, **kw: True
    adapter.set_message_handler(lambda event: None)
    dispatched = []
    adapter._start_session_processing = lambda event, key: dispatched.append((event, key)) or True
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    event = MessageEvent(text="during restore", source=source, platform_update_id=1)
    await adapter.handle_message(event)
    event._hermes_startup_restore_replay = True
    await adapter.handle_message(event)
    assert [item[0] for item in dispatched] == [event, event]
    await adapter.handle_message(MessageEvent(text=event.text, source=source, platform_update_id=1))
    assert len(dispatched) == 2
    with store.connect() as db:
        rows = db.execute("SELECT owner_id,state,payload FROM inbox").fetchall()
    assert [(row["owner_id"], row["state"], row["payload"]) for row in rows] == [(owner.id, "accepted", b"{}")]


@pytest.mark.asyncio
async def test_session_claim_releases_only_after_dependent_work_drains(tmp_path, monkeypatch):
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "slot-a")
    new = _identity("b", "slot-b")
    store.register(old, state="serving")
    store.register(new, state="standby")
    old_epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    store.claim_session(str(tmp_path), "telegram", key, old.id, old_epoch, outstanding_work=1)
    store.request_transfer(old.id, new.id, old_epoch, set())
    new_epoch = store.commit_transfer(old.id, new.id, old_epoch)
    live = {key: object()}
    adapter = SimpleNamespace(platform=Platform.TELEGRAM, _active_sessions=live, _pending_messages={})
    runner = SimpleNamespace(adapters={"telegram": adapter}, _overlap_draining=True, _pending_approvals={},
                             _active_work_count=lambda: int(bool(live)))
    generation = SimpleNamespace(coordinator=store, identity=old, epoch=old_epoch,
                                 runner=runner, home=tmp_path)
    routing = OwnedRouting(generation)
    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, "has_any_active", lambda: False)
    monkeypatch.setattr(process_registry, "pending_watchers", [])

    await routing._drain_once()
    with store.connect() as db:
        claim = db.execute("SELECT generation_id,outstanding_work FROM sessions WHERE session_key=?", (key,)).fetchone()
        assert tuple(claim) == (old.id, 1)

    live.clear()
    await routing._drain_once()
    with store.connect() as db:
        claim = db.execute("SELECT generation_id,epoch,outstanding_work FROM sessions WHERE session_key=?", (key,)).fetchone()
        assert tuple(claim) == (new.id, new_epoch, 0)


@pytest.mark.asyncio
async def test_live_existing_claim_is_frozen_before_successor_admission(tmp_path):
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "slot-a")
    new = _identity("b", "slot-b")
    store.register(old, state="serving")
    store.register(new, state="standby")
    epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    store.claim_session(str(tmp_path), "telegram", key, old.id, epoch)
    adapter = SimpleNamespace(platform=Platform.TELEGRAM, _active_sessions={key: object()}, _pending_messages={})
    runner = SimpleNamespace(adapters={"telegram": adapter})
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=epoch,
                                           runner=runner, home=tmp_path))
    routing.claim_live()
    store.request_transfer(old.id, new.id, epoch, set())
    new_epoch = store.commit_transfer(old.id, new.id, epoch)
    row, fresh = store.enqueue(str(tmp_path), "telegram", key, "next", "message",
                               json.dumps({"version": 1, "authorized": True, "sender": "1"}).encode(),
                               b"next", new.id, new_epoch)
    assert fresh and row["owner_id"] == old.id


@pytest.mark.parametrize("kind", [MessageType.PHOTO, MessageType.VOICE, MessageType.DOCUMENT])
@pytest.mark.asyncio
async def test_media_admission_locally_and_across_overlap(tmp_path, kind):
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "slot-a")
    new = _identity("b", "slot-b")
    store.register(old, state="serving")
    store.register(new, state="standby")
    epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    media = tmp_path / f"cached-{kind.value}"
    media.write_bytes(b"cached")
    adapter = SimpleNamespace(_is_sender_authorized=lambda *a, **kw: True)
    runner = SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)
    old_route = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=epoch, runner=runner))
    def event(update):
        return MessageEvent(text="media", source=source, message_type=kind, raw_message=object(),
                            platform_update_id=update, media_urls=[str(media)], media_types=["application/octet-stream"])
    assert await old_route.route_message(adapter, event(1), key) is False
    store.freeze_session(str(tmp_path), "telegram", key, old.id, epoch)
    store.request_transfer(old.id, new.id, epoch, set())
    new_epoch = store.commit_transfer(old.id, new.id, epoch)
    store.claim_session(str(tmp_path), "telegram", key, old.id, epoch, outstanding_work=1)
    new_route = OwnedRouting(SimpleNamespace(coordinator=store, identity=new, epoch=new_epoch, runner=runner))
    assert await new_route.route_message(adapter, event(2), key) is True
    with store.connect() as db:
        row = db.execute("SELECT payload FROM inbox WHERE source_event_id='2'").fetchone()
    assert json.loads(row["payload"])["event"]["media_urls"] == [str(media)]
    receiver = SimpleNamespace(build_source=lambda **kw: SessionSource(platform=Platform.TELEGRAM, **kw),
                               _canonicalize=lambda s: None)
    replayed = _restore_event(json.dumps(json.loads(row["payload"])["event"]), receiver)
    assert replayed.media_urls == [str(media)] and replayed.message_type == kind
    assert media.read_bytes() == b"cached"




@pytest.mark.asyncio
async def test_unrelated_background_process_does_not_pin_idle_session(tmp_path, monkeypatch):
    from tools.process_registry import process_registry
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "slot-a")
    new = _identity("b", "slot-b")
    store.register(old, state="serving")
    store.register(new, state="standby")
    epoch = store.acquire_lease("active_generation", old.id)
    idle = "agent:default:telegram:idle"
    busy = "agent:default:telegram:busy"
    for key in (idle, busy):
        store.claim_session(str(tmp_path), "telegram", key, old.id, epoch, outstanding_work=1)
    store.request_transfer(old.id, new.id, epoch, set())
    new_epoch = store.commit_transfer(old.id, new.id, epoch)
    monkeypatch.setattr(process_registry, "has_any_active", lambda: True)
    monkeypatch.setattr(process_registry, "has_active_for_session", lambda key: key == busy)
    monkeypatch.setattr(process_registry, "pending_watchers", [])
    runner = SimpleNamespace(adapters={}, _overlap_draining=True, _pending_approvals={}, _active_work_count=lambda: 0)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=epoch,
                                           runner=runner, home=tmp_path))
    await routing._drain_once()
    with store.connect() as db:
        claims = {r["session_key"]: r["generation_id"] for r in db.execute("SELECT * FROM sessions")}
    assert claims == {idle: new.id, busy: old.id}








@pytest.mark.asyncio
async def test_idle_drain_backs_off_and_overlap_returns_to_fast_poll(tmp_path, monkeypatch):
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "slot-a")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    runner = SimpleNamespace(adapters={}, _overlap_draining=False)
    generation = SimpleNamespace(coordinator=store, identity=owner, epoch=epoch,
                                 runner=runner, _drain_stopping=False)
    routing = OwnedRouting(generation)
    delays = []
    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            runner._overlap_draining = True
        if len(delays) == 4:
            generation._drain_stopping = True
    monkeypatch.setattr("gateway.owned_routing.asyncio.sleep", sleep)
    await routing.drain()
    assert delays[0] < delays[1] < delays[2]
    assert delays[3] == .1


@pytest.mark.asyncio
async def test_local_admission_placeholder_is_not_replayed(tmp_path):
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "slot-a")
    store.register(old, state="serving")
    epoch = store.acquire_lease("active_generation", old.id)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    adapter = SimpleNamespace(_is_sender_authorized=lambda *a, **kw: True,
                              _owner_transport_profile=lambda: None, platform=Platform.TELEGRAM,
                              _active_sessions={}, _pending_messages={})
    runner = SimpleNamespace(adapters={"telegram": adapter}, _resolve_profile_home_for_source=lambda s: tmp_path,
                             _overlap_draining=False)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=epoch, runner=runner))
    row, fresh = store.enqueue(str(tmp_path), "telegram", "chat", "1", "message",
                               json.dumps({"version": 1, "authorized": True, "sender": "1"}).encode(),
                               lambda: b"should-not-serialize", old.id, epoch)
    assert fresh and row["payload"] == b"{}"
    await routing._drain_once()
    with store.connect() as db:
        assert db.execute("SELECT state FROM inbox WHERE id=?", (row["id"],)).fetchone()[0] == "accepted"


@pytest.mark.asyncio
async def test_pending_replay_precedes_new_local_update_without_duplicates(tmp_path):
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "slot-a")
    owner = _identity("b", "slot-b")
    store.register(old, state="serving")
    store.register(owner, state="standby")
    old_epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    handled = []
    adapter = SimpleNamespace(
        platform=Platform.TELEGRAM, _is_sender_authorized=lambda *a, **kw: True,
        _owner_transport_profile=lambda: None, _active_sessions={}, _pending_messages={},
        build_source=lambda **kw: SessionSource(platform=Platform.TELEGRAM, **kw),
        _canonicalize=lambda s: setattr(s, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path)),
        _source_session_key=lambda s: key,
    )
    async def handle(event):
        handled.append(event.text)
        event._gateway_accepted = True
    adapter.handle_message = handle
    runner = SimpleNamespace(adapters={"telegram": adapter},
        _resolve_profile_home_for_source=lambda s: tmp_path, _overlap_draining=False)
    def event(text, update_id):
        return MessageEvent(text=text, source=source, message_type=MessageType.TEXT,
                            platform_update_id=update_id)
    envelope = json.dumps({"version": 1, "authorized": True, "sender": "1", "chat": "1",
                           "transport_profile": "default", "home": str(tmp_path)}).encode()
    from gateway.owned_routing import _event_payload
    store.enqueue(str(tmp_path), "telegram", key, "1", "message", envelope,
        json.dumps({"event": _event_payload(event("replay", 1))}).encode(), old.id, old_epoch)
    store.request_transfer(old.id, owner.id, old_epoch, set())
    epoch = store.commit_transfer(old.id, owner.id, old_epoch)
    assert store.transfer_session(str(tmp_path), "telegram", key, old.id, old_epoch, owner.id, epoch)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch, runner=runner))
    assert await routing.route_message(adapter, event("new", 2), key) is True
    assert await routing.route_message(adapter, event("new", 2), key) is True
    assert handled == []
    await routing._drain_once()
    await routing._drain_once()
    assert handled == ["replay", "new"]
    with store.connect() as db:
        assert [(r["source_event_id"], r["state"]) for r in db.execute(
            "SELECT source_event_id,state FROM inbox ORDER BY seq")] == [("1", "accepted"), ("2", "accepted")]


@pytest.mark.asyncio
async def test_refused_disposition_after_dispatch_interrupts_instead_of_replaying(tmp_path, monkeypatch, caplog):
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "slot-a")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    key = "agent:default:telegram:chat-1"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    from gateway.owned_routing import _event_payload
    event = MessageEvent(text="once", source=source, message_type=MessageType.TEXT, platform_update_id=1)
    envelope = json.dumps({"version": 1, "authorized": True, "sender": "1", "chat": "1",
                           "transport_profile": "default", "home": str(tmp_path)}).encode()
    row, _ = store.enqueue(str(tmp_path), "telegram", key, "1", "message", envelope,
        json.dumps({"event": _event_payload(event)}).encode(), owner.id, epoch)
    seen = []
    async def handle(evt):
        seen.append(evt.text)
        evt._gateway_accepted = True
    adapter = SimpleNamespace(platform=Platform.TELEGRAM, _active_sessions={}, _pending_messages={},
        _owner_transport_profile=lambda: None,
        build_source=lambda **kw: SessionSource(platform=Platform.TELEGRAM, **kw),
        _canonicalize=lambda s: setattr(s, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path)),
        _is_sender_authorized=lambda *a, **kw: True, _source_session_key=lambda s: key,
        handle_message=handle)
    runner = SimpleNamespace(adapters={"telegram": adapter},
        _resolve_profile_home_for_source=lambda s: tmp_path, _overlap_draining=False)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch, runner=runner))
    monkeypatch.setattr(store, "disposition", lambda *a: False)
    await routing._drain_once()
    await routing._drain_once()
    assert seen == ["once"]
    with store.connect() as db:
        assert db.execute("SELECT state FROM inbox WHERE id=?", (row["id"],)).fetchone()[0] == "interrupted"
    assert "interrupting row" in caplog.text


@pytest.mark.asyncio
async def test_callback_replay_marker_is_scoped_to_its_task():
    inside = asyncio.Event()
    release = asyncio.Event()
    async def replay():
        token = _owned_callback_replay.set(True)
        try:
            inside.set()
            await release.wait()
            assert _owned_callback_replay.get()
        finally:
            _owned_callback_replay.reset(token)
    task = asyncio.create_task(replay())
    await inside.wait()
    assert not _owned_callback_replay.get()
    release.set()
    await task
    assert not _owned_callback_replay.get()


@pytest.mark.asyncio
async def test_unauthorized_dm_falls_through_to_pairing_once(tmp_path):
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "slot-a")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="999", chat_type="dm", user_id="999")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    adapter = SimpleNamespace(_is_sender_authorized=lambda *a, **kw: False)
    runner = SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch, runner=runner))
    replies = []
    async def native(event):
        if not await routing.route_message(adapter, event, "chat"):
            replies.append("pairing code")
    await native(MessageEvent(text="hello", source=source, platform_update_id=7))
    assert replies == ["pairing code"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_two_goal_prompts_without_update_ids_both_reach_native_path(tmp_path):
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "slot-a")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    adapter = SimpleNamespace(_is_sender_authorized=lambda *a, **kw: True)
    runner = SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch, runner=runner))
    processed = []
    for text in ("goal continuation 1", "goal continuation 2"):
        event = MessageEvent(text=text, source=source)
        if not await routing.route_message(adapter, event, "chat"):
            processed.append(event.text)
    assert processed == ["goal continuation 1", "goal continuation 2"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_redispatched_local_event_survives_placeholder_without_second_copy(tmp_path):
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "slot-a")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    adapter = SimpleNamespace(_is_sender_authorized=lambda *a, **kw: True)
    runner = SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch, runner=runner))
    event = MessageEvent(text="held", source=source, platform_update_id=13)
    assert await routing.route_message(adapter, event, "chat") is False
    # The first native dispatch was cancelled before processing; Telegram holds
    # the same event object and re-dispatches it on reconnect.
    processed = []
    if not await routing.route_message(adapter, event, "chat"):
        processed.append(event.text)
    assert processed == ["held"]
    assert await routing.route_message(adapter, MessageEvent(text="held", source=source,
        platform_update_id=13), "chat") is True
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_unresolved_callback_is_answered_without_dispatch(tmp_path):
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "slot-a")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    answers = []
    async def answer(**kwargs):
        answers.append(kwargs)
    update = SimpleNamespace(update_id=14, callback_query=SimpleNamespace(answer=answer))
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch))
    assert await routing.route_callback(None, update, source, "chat") is True
    assert len(answers) == 1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0


def test_buffered_text_photo_and_group_claim_session_keys(tmp_path):
    from gateway.platforms.base import BasePlatformAdapter
    from plugins.platforms.telegram.adapter import TelegramAdapter

    key = "agent:default:telegram:chat-1"
    event = MessageEvent(text="part", source=SessionSource(platform=Platform.TELEGRAM, chat_id="1"))
    adapter = SimpleNamespace(
        platform=Platform.TELEGRAM, _active_sessions={}, _pending_messages={},
        _pending_text_batches={key: event},
        _pending_photo_batches={f"{key}:album:42": event},
        _media_group_events={"42": event},
        _event_session_key=lambda e: key,
    )
    # These are the actual key shapes supplied by the base text batcher and
    # Telegram's photo/group batchers, not three copies of the session key.
    assert BasePlatformAdapter._text_batch_key(adapter, event) == key
    assert TelegramAdapter._photo_batch_key(adapter, event, SimpleNamespace(media_group_id="42")) == f"{key}:album:42"
    route = OwnedRouting(SimpleNamespace(runner=SimpleNamespace(adapters={"telegram": adapter}, _pending_approvals={})))
    assert route._live_keys() == {key}


def test_unknown_event_fields_are_ignored_on_replay():
    from gateway.owned_routing import _event_payload
    event = MessageEvent(text="hello", source=SessionSource(platform=Platform.TELEGRAM, chat_id="1"),
                         platform_update_id=3)
    payload = _event_payload(event)
    payload["future_wire_field"] = "safe to ignore"
    adapter = SimpleNamespace(build_source=lambda **kw: SessionSource(platform=Platform.TELEGRAM, **kw),
                              _canonicalize=lambda source: None)
    restored = _restore_event(json.dumps(payload), adapter)
    assert restored.text == event.text and restored.platform_update_id == event.platform_update_id


@pytest.mark.asyncio
async def test_observed_group_without_sender_uses_native_owner_or_routes_to_old(tmp_path):
    store = GenerationCoordinator(tmp_path)
    old, new = _identity("a", "old"), _identity("b", "new")
    store.register(old, state="serving")
    store.register(new, state="standby")
    old_epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:group-1"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="group", user_id=None)
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    adapter = SimpleNamespace(_is_sender_authorized=lambda *a, **kw: False)
    runner = SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)
    old_route = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=old_epoch, runner=runner))
    event = lambda update: MessageEvent(text="observed", source=source, platform_update_id=update)
    assert await old_route.route_message(adapter, event(1), key) is False
    store.freeze_session(str(tmp_path), "telegram", key, old.id, old_epoch)
    store.request_transfer(old.id, new.id, old_epoch, set())
    new_epoch = store.commit_transfer(old.id, new.id, old_epoch)
    new_route = OwnedRouting(SimpleNamespace(coordinator=store, identity=new, epoch=new_epoch, runner=runner))
    assert await new_route.route_message(adapter, event(2), key) is True
    with store.connect() as db:
        row = db.execute("SELECT owner_id FROM inbox WHERE source_event_id='2'").fetchone()
    assert row["owner_id"] == old.id


@pytest.mark.asyncio
async def test_bot_authored_allowed_message_retains_identity_on_owner_replay(tmp_path):
    from gateway.owned_routing import _event_payload
    store = GenerationCoordinator(tmp_path)
    old, new = _identity("a", "old"), _identity("b", "new")
    store.register(old, state="serving")
    store.register(new, state="standby")
    old_epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="7", is_bot=True)
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    event = MessageEvent(text="bot message", source=source, platform_update_id=2)
    store.claim_session(str(tmp_path), "telegram", key, old.id, old_epoch, outstanding_work=1)
    store.request_transfer(old.id, new.id, old_epoch, set())
    new_epoch = store.commit_transfer(old.id, new.id, old_epoch)
    adapter = SimpleNamespace(platform=Platform.TELEGRAM, _active_sessions={}, _pending_messages={},
        _owner_transport_profile=lambda: None,
        _is_sender_authorized=lambda *a, **kw: True,
        build_source=lambda **kw: SessionSource(platform=Platform.TELEGRAM, **kw),
        _canonicalize=lambda s: setattr(s, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path)),
        _source_session_key=lambda s: key)
    runner = SimpleNamespace(adapters={"telegram": adapter}, _resolve_profile_home_for_source=lambda s: tmp_path,
                             _overlap_draining=False)
    route = OwnedRouting(SimpleNamespace(coordinator=store, identity=new, epoch=new_epoch, runner=runner))
    assert await route.route_message(adapter, event, key) is True
    with store.connect() as db:
        row = db.execute("SELECT * FROM inbox WHERE source_event_id='2'").fetchone()
    assert row["owner_id"] == old.id
    assert json.loads(row["authorized_source"])["is_bot"] is True
    restored = _restore_event(json.dumps(json.loads(row["payload"])["event"]), adapter)
    assert restored.source.is_bot is True
    seen = []
    async def handle(replayed):
        seen.append((replayed.source.is_bot, replayed.text))
        replayed._gateway_accepted = True
    adapter.handle_message = handle
    store.set_outstanding(str(tmp_path), "telegram", key, old.id, old_epoch, 0)
    store.transfer_session(str(tmp_path), "telegram", key, old.id, old_epoch, new.id, new_epoch)
    await route._drain_once()
    assert seen == [(True, "bot message")]


@pytest.mark.asyncio
async def test_late_dispatch_for_frozen_session_stays_native_on_draining_owner(tmp_path):
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "old")
    new = _identity("b", "new")
    store.register(old, state="serving")
    store.register(new, state="standby")
    old_epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    store.freeze_session(str(tmp_path), "telegram", key, old.id, old_epoch)
    store.request_transfer(old.id, new.id, old_epoch, set())
    store.commit_transfer(old.id, new.id, old_epoch)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    event = MessageEvent(text="late", source=source, platform_update_id=9)
    adapter = SimpleNamespace(_is_sender_authorized=lambda *a, **kw: True)
    runner = SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path, _overlap_draining=True)
    route = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=old_epoch, runner=runner))
    assert await route.route_message(adapter, event, key) is False
    event._owned_local_pending = None  # The native handler completed its dispatch.
    assert await route.route_message(adapter, event, key) is True
    with store.connect() as db:
        rows = db.execute("SELECT owner_id,state FROM inbox").fetchall()
        assert [(row["owner_id"], row["state"]) for row in rows] == [(old.id, "accepted")]


@pytest.mark.parametrize("idle_claim", [False, True])
@pytest.mark.asyncio
async def test_late_dispatch_for_idle_or_unowned_session_reaches_successor_once(tmp_path, idle_claim):
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "old")
    new = _identity("b", "new")
    store.register(old, state="serving")
    store.register(new, state="standby")
    old_epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:new"
    if idle_claim:
        store.claim_session(str(tmp_path), "telegram", key, old.id, old_epoch)
    store.request_transfer(old.id, new.id, old_epoch, set())
    new_epoch = store.commit_transfer(old.id, new.id, old_epoch)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    event = MessageEvent(text="unowned", source=source, platform_update_id=10)
    key = "agent:default:telegram:new"
    processed = []
    adapter = SimpleNamespace(
        platform=Platform.TELEGRAM, _active_sessions={}, _pending_messages={},
        _is_sender_authorized=lambda *a, **kw: True, _owner_transport_profile=lambda: None,
        build_source=lambda **kw: SessionSource(platform=Platform.TELEGRAM, **kw),
        _canonicalize=lambda s: setattr(s, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path)),
        _source_session_key=lambda s: key,
    )
    async def handle(replayed):
        processed.append((new.id, replayed.text))
        replayed._gateway_accepted = True
    adapter.handle_message = handle
    runner = SimpleNamespace(adapters={"telegram": adapter},
        _resolve_profile_home_for_source=lambda s: tmp_path, _overlap_draining=True)
    route = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=old_epoch, runner=runner))
    assert await route.route_message(adapter, event, key) is True
    assert await route.route_message(adapter, event, key) is True
    assert processed == []
    with store.connect() as db:
        assert db.execute("SELECT generation_id FROM sessions WHERE session_key=?", (key,)).fetchone()[0] == new.id
    runner._overlap_draining = False
    new_route = OwnedRouting(SimpleNamespace(coordinator=store, identity=new, epoch=new_epoch, runner=runner))
    # B's next enqueue must join the same replay lane, never overlap a native A dispatch.
    assert await new_route.route_message(adapter, MessageEvent(text="next", source=source,
        platform_update_id=11), key) is True
    await new_route._drain_once()
    await new_route._drain_once()
    assert processed == [(new.id, "unowned"), (new.id, "next")]


@pytest.mark.parametrize("frozen", [True, False])
@pytest.mark.asyncio
async def test_late_callback_preserves_owned_session_or_forwards_to_successor(tmp_path, frozen):
    store = GenerationCoordinator(tmp_path)
    old, new = _identity("a", "old"), _identity("b", "new")
    store.register(old, state="serving")
    store.register(new, state="standby")
    epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    if frozen:
        store.freeze_session(str(tmp_path), "telegram", key, old.id, epoch)
    store.request_transfer(old.id, new.id, epoch, set())
    store.commit_transfer(old.id, new.id, epoch)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
    setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
    route = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=epoch,
        runner=SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)))
    update = SimpleNamespace(update_id=11, to_dict=lambda: {"update_id": 11})
    assert await route.route_callback(SimpleNamespace(), update, source, key) is (not frozen)
    assert await route.route_callback(SimpleNamespace(), update, source, key) is True
    with store.connect() as db:
        rows = db.execute("SELECT owner_id,state,payload FROM inbox").fetchall()
    assert len(rows) == 1
    assert rows[0]["owner_id"] == (old.id if frozen else new.id)
    assert rows[0]["state"] == ("accepted" if frozen else "pending")
    if not frozen:
        assert json.loads(rows[0]["payload"])["callback"] == {"update_id": 11}


@pytest.mark.asyncio
async def test_inbox_read_keeps_event_loop_responsive(tmp_path, monkeypatch):
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "owner")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    route = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch,
        runner=SimpleNamespace(adapters={}, _overlap_draining=False)))
    original = store._transaction
    entered, release = threading.Event(), threading.Event()
    def slow_transaction():
        entered.set()
        release.wait(2)
        return original()
    monkeypatch.setattr(store, "_transaction", slow_transaction)
    task = asyncio.create_task(route._drain_once())
    try:
        # A concurrent ticker must run while the SQLite connection is blocked.
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
        tick = asyncio.create_task(asyncio.sleep(.05))
        await asyncio.wait_for(tick, .5)
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_anonymous_admin_event_drops_without_reopening(tmp_path):
    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "slot-a")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id=None)
    adapter = SimpleNamespace(_is_sender_authorized=lambda *a, **kw: False)
    runner = SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch, runner=runner))
    assert await routing.route_message(adapter, MessageEvent(text="anonymous", source=source), "chat") is True


@pytest.mark.asyncio
async def test_settled_inbox_retention_preserves_replay_and_native_obligations(tmp_path, monkeypatch):
    from plugins.platforms.telegram.polling_transfer import PollingJournal

    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "owner")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    journal = PollingJournal(store, "test-token")
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    rows = {}
    for event_id, state, wire_state, key in (
        (1, "accepted", "accepted", "settled"),
        (2, "pending", "accepted", "pending"),
        (3, "processing", "accepted", "processing"),
        (4, "accepted", "processing", "wire-processing"),
        (5, "accepted", "received", "replayable"),
        (6, "accepted", "accepted", "live"),
        (8, "accepted", "accepted", "legacy"),
        (9, "accepted", "accepted", "outstanding"),
    ):
        journal.record_response(json.dumps({"ok": True, "result": [{"update_id": event_id}]}).encode())
        if wire_state != "received":
            assert await journal.claim(event_id)
            if wire_state == "accepted":
                await journal.accept(event_id)
        envelope = {"version": 1, "authorized": True, "sender": "1"}
        if key != "legacy":
            envelope["token_hash"] = journal.token_hash
        if event_id == 1:
            source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1")
            setattr(source, "_identity", RoutingIdentity("default", "default", tmp_path, tmp_path))
            native = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch,
                runner=SimpleNamespace(_resolve_profile_home_for_source=lambda s: tmp_path)))
            assert await native.route_message(SimpleNamespace(_controlled_journal=journal,
                _is_sender_authorized=lambda *a, **kw: True),
                MessageEvent(text="settled", source=source, platform_update_id=1), key) is False
            with store.connect() as db:
                row = dict(db.execute("SELECT * FROM inbox WHERE source_event_id='1'").fetchone())
            assert json.loads(row["authorized_source"])["token_hash"] == journal.token_hash
        else:
            row, _ = store.enqueue(str(tmp_path), "telegram", key, str(event_id), "message",
                                   json.dumps(envelope).encode(), lambda: b"unused", owner.id, epoch)
        rows[event_id] = row
        with store.connect() as db:
            db.execute("UPDATE inbox SET state=? WHERE id=?", (state, row["id"]))
    store.set_outstanding(str(tmp_path), "telegram", "outstanding", owner.id, epoch, 1)
    clock += 8 * 86400
    recent, _ = store.enqueue(str(tmp_path), "telegram", "recent", "7", "message",
        json.dumps({"version": 1, "authorized": True, "sender": "1", "token_hash": journal.token_hash}).encode(),
        lambda: b"unused", owner.id, epoch)
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=owner, epoch=epoch,
        runner=SimpleNamespace(adapters={}, _overlap_draining=False)))
    monkeypatch.setattr(routing, "_live_keys", lambda: {"live"})
    await routing._drain_once()
    with store.connect() as db:
        retained = {int(row[0]) for row in db.execute("SELECT source_event_id FROM inbox")}
    assert retained == {2, 3, 4, 5, 6, 7, 8, 9}
    # The transport gate still refuses the expired settled update. Retained
    # receipts still deduplicate, and pending payloads remain available.
    assert not await journal.claim(1)
    assert await journal.claim(5)
    duplicate, fresh = store.enqueue(str(tmp_path), "telegram", "recent", "7", "message",
        json.dumps({"version": 1, "authorized": True, "sender": "1"}).encode(), b"changed", owner.id, epoch)
    assert not fresh and duplicate["id"] == recent["id"]
    assert [row["id"] for row in store.pending(owner.id, str(tmp_path), "telegram", "pending")] == [rows[2]["id"]]


def test_existing_inbox_gains_retention_clock_without_losing_receipts(tmp_path):
    import sqlite3
    from contextlib import closing

    store = GenerationCoordinator(tmp_path)
    owner = _identity("a", "owner")
    store.register(owner, state="serving")
    epoch = store.acquire_lease("active_generation", owner.id)
    row, _ = store.enqueue(str(tmp_path), "telegram", "chat", "1", "message",
        json.dumps({"version": 1, "authorized": True, "sender": "1"}).encode(), b"pending", owner.id, epoch)
    # Exercise the pre-retention schema, not a source-text copy of its DDL.
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP INDEX IF EXISTS inbox_retention")
        if "created_at" in {row[1] for row in db.execute("PRAGMA table_info(inbox)")}:
            db.execute("ALTER TABLE inbox DROP COLUMN created_at")
    reopened = GenerationCoordinator(tmp_path)
    pending = reopened.pending(owner.id, str(tmp_path), "telegram", "chat")
    assert len(pending) == 1 and pending[0]["id"] == row["id"]
    assert pending[0]["payload"] == b"pending" and pending[0]["created_at"] > 0
    duplicate, fresh = reopened.enqueue(str(tmp_path), "telegram", "chat", "1", "message",
        json.dumps({"version": 1, "authorized": True, "sender": "1"}).encode(), b"changed", owner.id, epoch)
    assert not fresh and duplicate == pending[0]


@pytest.fixture(autouse=True)
def _coordinator_boot_identity(monkeypatch):
    # Unit transactions use a stable supplied boot identity. Native process
    # and launchd suites continue to probe the actual host.
    monkeypatch.setattr("gateway.generation._boot_id", lambda: "unit-test-boot")
