"""Generation-owned routing preserves obligations until the owner is truly idle."""
import asyncio
import json
import os
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


@pytest.mark.asyncio
async def test_session_claim_releases_only_after_dependent_work_drains(tmp_path, monkeypatch):
    store = GenerationCoordinator(tmp_path)
    old = _identity("a", "slot-a")
    new = _identity("b", "slot-b")
    store.register(old, state="draining")
    store.register(new, state="serving")
    old_epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    store.claim_session(str(tmp_path), "telegram", key, old.id, old_epoch, outstanding_work=1)
    assert store.release_lease("active_generation", old.id, old_epoch)
    new_epoch = store.acquire_lease("active_generation", new.id)
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
    store.register(old, state="draining")
    store.register(new, state="serving")
    epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    store.claim_session(str(tmp_path), "telegram", key, old.id, epoch)
    adapter = SimpleNamespace(platform=Platform.TELEGRAM, _active_sessions={key: object()}, _pending_messages={})
    runner = SimpleNamespace(adapters={"telegram": adapter})
    routing = OwnedRouting(SimpleNamespace(coordinator=store, identity=old, epoch=epoch,
                                           runner=runner, home=tmp_path))
    routing.claim_live()
    assert store.release_lease("active_generation", old.id, epoch)
    new_epoch = store.acquire_lease("active_generation", new.id)
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
    store.register(new, state="serving")
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
    assert store.release_lease("active_generation", old.id, epoch)
    store.heartbeat(old.id, state="draining")
    new_epoch = store.acquire_lease("active_generation", new.id)
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
    store.register(old, state="draining")
    store.register(new, state="serving")
    epoch = store.acquire_lease("active_generation", old.id)
    idle = "agent:default:telegram:idle"
    busy = "agent:default:telegram:busy"
    for key in (idle, busy):
        store.claim_session(str(tmp_path), "telegram", key, old.id, epoch, outstanding_work=1)
    assert store.release_lease("active_generation", old.id, epoch)
    new_epoch = store.acquire_lease("active_generation", new.id)
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
        assert db.execute("SELECT state FROM inbox WHERE id=?", (row["id"],)).fetchone()[0] == "pending"


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
