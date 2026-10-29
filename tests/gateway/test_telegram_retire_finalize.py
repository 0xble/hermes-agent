"""Already-absent topics finish native retirement without deleting transcript history."""
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import telegram_retire as retire
from gateway import telegram_retire_work as work
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    thread_id = getattr(request, "param", "17")
    db = SessionDB(tmp_path / "state.db")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._db = db
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="101", thread_id=thread_id, chat_type="dm", user_id="101")
    entry = store.get_or_create_session(source)
    db.bind_telegram_topic(chat_id="101", thread_id=thread_id, user_id="101",
                           session_key=entry.session_key, session_id=entry.session_id)
    db.append_message(entry.session_id, "user", "Keep this transcript.")
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="bot")),
                          send_message=AsyncMock(), delete_forum_topic=AsyncMock())
    adapter = SimpleNamespace(_bot=bot, send=AsyncMock(), _dm_topics={"101:Retired": "17"})
    runner = SimpleNamespace(_session_db=AsyncSessionDB(db), session_store=store,
                             _session_key_for_source=store._generate_session_key,
                             _delivery_adapter_for=lambda _: adapter,
                             _is_session_running=lambda _: False,
                             _async_profile_scope_for_source=lambda _: nullcontext())
    state = {"phase": "failed", "generation": "gen", "session_id": entry.session_id,
             "session_key": entry.session_key, "source": source.to_dict(), "handoffs": {},
             "error": "closing failed", "reason": "", "target": ""}
    monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[]))
    monkeypatch.setattr("hermes_platform.resolver.locate_command", lambda _: SimpleNamespace(command=("telegram",)))
    monkeypatch.setattr(work, "cli", AsyncMock(return_value=[]))
    yield SimpleNamespace(db=db, runner=runner, source=source, state=state, adapter=adapter, bot=bot, entry=entry)
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", ["bound", "dm_removed", "route_removed"])
async def test_restart_finalizes_absent_topic_with_partial_bindings(runtime, partial):
    r = runtime
    if partial != "bound":
        r.db.delete_telegram_topic_binding(chat_id="101", thread_id="17")
    if partial == "route_removed":
        assert r.runner.session_store.unbind_session(r.entry.session_key, r.entry.session_id)
    await retire.save(r.runner, r.source, r.state)
    before = r.db.get_messages(r.entry.session_id)
    await retire.resume_all(r.runner)
    state = await retire.load(r.runner, r.source)
    assert state is not None and state["phase"] == "done"
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is None
    assert r.runner.session_store.peek_session_id(r.entry.session_key) is None
    assert not r.db._read_all("SELECT * FROM gateway_routing WHERE session_key = ?", (r.entry.session_key,))
    assert r.db.get_session(r.entry.session_id)["session_key"] is None
    assert r.db.get_messages(r.entry.session_id) == before
    r.bot.send_message.assert_not_awaited()
    r.bot.delete_forum_topic.assert_not_awaited()
    r.adapter.send.assert_not_awaited()
    await retire.resume_all(r.runner)
    assert await retire.load(r.runner, r.source) == state
    assert r.db.get_messages(r.entry.session_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["present", "closed", "outage", "open_work", "active_successor", "rebound"])
async def test_absent_recovery_preserves_ambiguous_or_unsettled_topics(runtime, monkeypatch, block):
    r = runtime
    if block == "present":
        monkeypatch.setattr(work, "cli", AsyncMock(return_value=[{"id": "17"}]))
    elif block in {"closed", "outage"}:
        monkeypatch.setattr(work, "cli", AsyncMock(side_effect=RuntimeError(
            "topic closed" if block == "closed" else "network outage")))
    elif block == "open_work":
        monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[{"id": "loop", "kind": "loop"}]))
    elif block == "active_successor":
        monkeypatch.setattr(work, "receipt", AsyncMock(return_value={"receipt_id": "accepted"}))
        from hermes_cli import loops
        monkeypatch.setattr(loops, "_get_session_db", lambda: r.db)
        source_loop = {"status": "active", "prompt": "Continue source work"}
        r.db.set_meta(f"loop:{r.entry.session_id}", json.dumps(source_loop))
        r.db.create_session("next", source="telegram")
        r.db.set_meta("loop:next", json.dumps({"status": "active", "prompt": "Independent successor work"}))
        r.state["handoffs"] = {"loop": {"receipt_id": "accepted", "item": {"id": "loop", "kind": "loop", "record": source_loop},
                                        "destination": {"session_id": "next", "thread_id": "18"}}}
    elif block == "rebound":
        r.runner.session_store.get_or_create_session(r.source, force_new=True)
    await retire.save(r.runner, r.source, r.state)
    await retire.resume_all(r.runner)
    assert (await retire.load(r.runner, r.source))["phase"] == "failed"
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is not None
    assert r.db.get_session(r.entry.session_id)["session_key"] == r.entry.session_key
    r.bot.send_message.assert_not_awaited()
    r.bot.delete_forum_topic.assert_not_awaited()
    r.adapter.send.assert_not_awaited()
    if block == "active_successor":
        assert json.loads(r.db.get_meta("loop:next"))["prompt"] == "Independent successor work"
        assert json.loads(r.db.get_meta(f"loop:{r.entry.session_id}"))["status"] == "active"

@pytest.mark.asyncio
async def test_absent_merge_waits_for_existing_receipt_without_replaying_send(runtime, monkeypatch):
    r = runtime
    item = {"id": "context:next", "kind": "context", "record": {"summary": "Preserve original limits."}}
    dest = {"session_id": "next", "thread_id": "18", "session_key": "next-key"}
    r.state.update(target="Successor", merge_destination=dest,
                   handoffs={item["id"]: {"item": item, "destination": dest, "moved": False,
                                          "receipt_id": "r1", "address": "hermes:default/next"}})
    delivered = False

    async def cli(*args, **kwargs):
        if args[2] == "telegram":
            return []
        assert args[2:5] == ("relay", "--json", "status")
        return {"id": "r1", "target": "hermes:default/next", "status": "replied" if delivered else "posted"}

    command = AsyncMock(side_effect=cli)
    monkeypatch.setattr(work, "cli", command)
    await retire.save(r.runner, r.source, r.state)
    await retire.resume_all(r.runner)
    assert (await retire.load(r.runner, r.source))["phase"] == "failed"
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is not None
    delivered = True
    await retire.resume_all(r.runner)
    final = await retire.load(r.runner, r.source)
    assert final is not None and final["phase"] == "done"
    assert final["handoffs"][item["id"]]["moved"] is True
    assert r.db.get_session(r.entry.session_id)["session_key"] is None
    assert [call.args[4] for call in command.await_args_list if call.args[2] == "relay"] == ["status", "status"]
    r.adapter.send.assert_not_awaited()
    r.bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["379709", "388380", "357517"], indirect=True)
async def test_absence_without_closing_receipt_has_truthful_disposition(runtime):
    r = runtime
    await retire.save(r.runner, r.source, r.state)
    await retire.resume_all(r.runner)
    final = await retire.load(r.runner, r.source)
    assert final is not None and final["phase"] == "done"
    assert "closing_receipt" not in final
    assert final["disposition"]["status"] == "absent_at_finalization"
    assert final["disposition"]["thread_id"] == r.source.thread_id
    assert final["disposition"]["chat_id"] == r.source.chat_id
    assert final["disposition"]["generation"] == r.state["generation"]
    assert final["error"] == "closing failed"
    r.adapter.send.assert_not_awaited()
    r.bot.send_message.assert_not_awaited()
    r.bot.delete_forum_topic.assert_not_awaited()


def test_partial_unbind_refuses_rebound_persistent_route(runtime):
    r = runtime
    r.db.create_session("new-owner", source="telegram")
    r.db.save_gateway_routing_entry(r.entry.session_key, json.dumps({"session_id": "new-owner"}), scope="other-scope")
    assert not r.db.unbind_gateway_session_peer(r.entry.session_key, r.entry.session_id)
    assert r.db.get_session(r.entry.session_id)["session_key"] == r.entry.session_key
    assert r.db.load_gateway_routing_entry(r.entry.session_key, scope="other-scope") is not None
    assert r.db.get_messages(r.entry.session_id)
