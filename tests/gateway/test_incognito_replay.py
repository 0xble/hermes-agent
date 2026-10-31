"""Mixed replay collection never turns one topic's privacy into another's policy."""
import asyncio

import pytest

from agent.incognito import is_incognito, register_session, tool_refusal
from gateway.config import GatewayConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionStore
from gateway.shutdown_flush import flush_pending_to_file, recover_pending_to_db
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from unittest.mock import Mock


@pytest.mark.asyncio
@pytest.mark.parametrize("private_first", [False, True])
async def test_mixed_replay_collection_and_worker_preserve_each_topics_privacy(tmp_path, monkeypatch, private_first):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.session_store = store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner._startup_restore_queue = []
    runner._startup_restore_keys = {}
    runner._startup_restore_claimed_keys = set()
    runner.config.restart_replay_concurrency = 1
    sources = [make_restart_source(chat_id="private-replay"), make_restart_source(chat_id="public-replay")]
    entries = [store.get_or_create_session(source) for source in sources]
    private, public = entries
    for entry in entries:
        store.append_to_transcript(entry.session_id, {"role": "user", "content": "saved public prefix"})
        store.append_to_transcript(entry.session_id, {"role": "assistant", "content": "saved public reply"})
    saved_prefix = store.load_transcript(private.session_id)
    store.set_incognito(private.session_key, True)
    saved_files = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and private.session_id in p.name}
    # Shutdown collection accepts public input but never creates a private spool.
    pending = {entry.session_key: MessageEvent(text=label, source=source, user_id=source.user_id)
               for entry, source, label in zip(entries, sources, ["PRIVATE_REPLAY_CANARY", "public queued input"])}
    seen = []

    async def handle(event):
        key = runner._session_key_for_source(event.source)
        entry = store.lookup_by_session_key(key)
        assert entry is not None
        seen.append((event.text, is_incognito(), tool_refusal("memory", {"action": "add"})))
        store.append_to_transcript(entry.session_id, {"role": "user", "content": event.text})
        store.append_to_transcript(entry.session_id, {"role": "assistant", "content": "synthetic reply"})
        event._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    try:
        for event in pending.values():
            event._drain_deferred = True
        runner._pending_messages = dict(pending)
        assert runner._persist_shutdown_pending_messages() == 1
        assert private.session_key in runner._pending_messages
        assert public.session_key not in runner._pending_messages
        assert all(b"PRIVATE_REPLAY_CANARY" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
        # Recovery collector hands public input to the actual scheduler. Also queue a
        # private, in-process event, as can happen while a restored topic turns private.
        from functools import partial
        from gateway.run_pending_recovery import _defer_followup
        recover_pending_to_db(Mock(), session_resolver=lambda *a, **k: (public.session_id, Mock()),
                              deferred_followup=partial(_defer_followup, runner, {}, None))
        private_event = pending[private.session_key]
        runner._queue_startup_restore_event(private_event, front=private_first, session_key=private.session_key)
        assert await runner._drain_startup_restore_queue() == 2
        await asyncio.gather(*list(adapter._background_tasks))
        await asyncio.gather(*list(runner._background_tasks))
        by_text = {text: (private_scope, refusal) for text, private_scope, refusal in seen}
        assert by_text["PRIVATE_REPLAY_CANARY"][0] is True
        assert by_text["PRIVATE_REPLAY_CANARY"][1] is not None
        assert by_text["public queued input"] == (False, None)
        assert any(m["content"] == "public queued input" for m in store.load_transcript(public.session_id))
        assert all(b"PRIVATE_REPLAY_CANARY" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
        scheduler = runner._get_replay_scheduler()
        await scheduler.wait_idle()
        await asyncio.gather(*tuple(scheduler._workers))
        assert not scheduler._workers
        # A new batch can be scheduled by a private turn, not just gateway startup.
        from agent.incognito import incognito_scope
        for source, entry, text in zip(sources, entries, ["PRIVATE_IDLE_CANARY", "public after idle"]):
            event = MessageEvent(text=text, source=source, user_id=source.user_id)
            runner._queue_startup_restore_event(event, session_key=entry.session_key)
        with incognito_scope(session_id=private.session_key):
            assert await runner._drain_startup_restore_queue() == 2
        await asyncio.gather(*list(adapter._background_tasks))
        await asyncio.gather(*list(runner._background_tasks))
        by_text = {text: (scope, refusal) for text, scope, refusal in seen}
        assert by_text["PRIVATE_IDLE_CANARY"][0] is True
        assert by_text["public after idle"] == (False, None)
        assert any(m["content"] == "public after idle" for m in store.load_transcript(public.session_id))
        assert {p: p.read_bytes() for p in saved_files} == saved_files
        restarted = SessionStore(tmp_path / "sessions", GatewayConfig())
        assert restarted.incognito_enabled(private.session_key)
        assert restarted.load_transcript(private.session_id) == saved_prefix
        assert any(m["content"] == "public after idle" for m in restarted.load_transcript(public.session_id))
    finally:
        await runner._get_replay_scheduler().close()
        for entry in entries:
            register_session(entry.session_id, False)
            register_session(entry.session_key, False)


@pytest.mark.asyncio
async def test_capped_replay_drop_and_shutdown_collector_spool_only_public(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, _adapter = make_restart_runner()
    runner.session_store = store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner._startup_restore_queue = []
    runner._startup_restore_keys = {}
    runner._startup_restore_claimed_keys = set()
    runner.config.restart_replay_concurrency = 1
    entries = [store.get_or_create_session(make_restart_source(chat_id=chat))
               for chat in ["private-drop", "public-drop"]]
    private, _public = entries
    store.set_incognito(private.session_key, True)
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        started.set()
        await release.wait()

    async def never_dispatch():
        pytest.fail("queued replay was dispatched past the cap")

    scheduler = runner._get_replay_scheduler()
    try:
        active = runner._enqueue_replay(priority=1, kind="active", session_key="blocker", dispatch=blocked)
        scheduler.start()
        await asyncio.wait_for(started.wait(), timeout=5)
        handles = []
        for entry, text in zip(entries, ["PRIVATE_DROPPED_CANARY", "public dropped input"]):
            event = MessageEvent(text=text, source=entry.origin, user_id=entry.origin.user_id)
            handles.append(runner._enqueue_replay(
                priority=1, kind="startup", session_key=entry.session_key, dispatch=never_dispatch,
                on_drop=lambda event=event, key=entry.session_key: runner._queue_startup_restore_event(event, session_key=key),
            ))
        assert scheduler.queue_depth == 2
        await scheduler.close(timeout=0)
        assert active.future.cancelled()
        assert all(handle.future.cancelled() for handle in handles)
        assert runner._persist_shutdown_pending_messages() == 1
        assert len(runner._startup_restore_queue) == 1
        assert runner._startup_restore_queue[0].text == "PRIVATE_DROPPED_CANARY"
        disk = [p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()]
        assert any(b"public dropped input" in data for data in disk)
        assert all(b"PRIVATE_DROPPED_CANARY" not in data for data in disk)
        restarted = SessionStore(tmp_path / "sessions", GatewayConfig())
        assert restarted.incognito_enabled(private.session_key)
        assert restarted.load_transcript(private.session_id) == []
    finally:
        release.set()
        await scheduler.close(timeout=0)
        for entry in entries:
            register_session(entry.session_id, False)
            register_session(entry.session_key, False)
