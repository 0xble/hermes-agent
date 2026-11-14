"""Already-absent topics finish native retirement without deleting transcript history."""
import json
import time
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
    async def admit(event):
        event._gateway_accepted = True
    adapter.handle_message = AsyncMock(side_effect=admit)
    runner = SimpleNamespace(_session_db=AsyncSessionDB(db), session_store=store,
                             _session_key_for_source=store._generate_session_key,
                             _delivery_adapter_for=lambda _: adapter,
                             _is_session_running=lambda _: False,
                             _async_profile_scope_for_source=lambda _: nullcontext())
    state = {"phase": "failed", "generation": "gen", "session_id": entry.session_id,
             "session_key": entry.session_key, "source": source.to_dict(), "handoffs": {},
             "error": "closing failed", "reason": "", "target": "", "failed_at": time.time()}
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=()))
    from cron import jobs
    from tools import async_delegation
    from tools.process_registry import process_registry
    from hermes_cli import goals, loops
    monkeypatch.setattr(jobs, "list_jobs", lambda **_: [])
    monkeypatch.setattr(async_delegation, "retirement_inventory", lambda *_: [])
    monkeypatch.setattr(process_registry, "list_sessions", lambda **_: [])
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    monkeypatch.setattr(loops, "_get_session_db", lambda: db)
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
        monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[{"id": "task:t1", "kind": "task"}]))
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
    if block == "active_successor":
        # The delivered handoff task carries the loop to its busy destination, whose own loop is
        # never overwritten, so the absent topic now finalizes.
        assert (await retire.load(r.runner, r.source))["phase"] == "done"
        assert json.loads(r.db.get_meta("loop:next"))["prompt"] == "Independent successor work"
        assert json.loads(r.db.get_meta(f"loop:{r.entry.session_id}"))["status"] == "cleared"
        return
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
    assert final["disposition"]["phase_at_finalization"] == "failed"
    assert final["error"] == "closing failed"
    r.adapter.send.assert_not_awaited()
    r.bot.send_message.assert_not_awaited()
    r.bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pending", "waiting"])
async def test_waiting_absent_topic_finalizes_without_closing(runtime, monkeypatch, phase):
    r = runtime
    r.state["phase"] = phase
    prepare = AsyncMock(return_value=False)
    monkeypatch.setattr(work, "prepare", prepare)
    await retire.save(r.runner, r.source, r.state)
    before = r.db.get_messages(r.entry.session_id)

    await retire.advance(r.runner, r.source)

    final = await retire.load(r.runner, r.source)
    assert final["phase"] == "done"
    assert final["disposition"]["status"] == "absent_at_finalization"
    assert final["disposition"]["phase_at_finalization"] == phase
    assert final["disposition"]["generation"] == r.state["generation"]
    assert final["disposition"]["chat_id"] == r.source.chat_id
    assert final["disposition"]["thread_id"] == r.source.thread_id
    assert final["disposition"]["observed_at"] > 0
    assert not final.get("closing_attempt") and not final.get("closing_receipt")
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is None
    assert r.runner.session_store.peek_session_id(r.entry.session_key) is None
    assert r.db.get_session(r.entry.session_id)["session_key"] is None
    assert r.db.get_messages(r.entry.session_id) == before
    prepare.assert_not_awaited()
    r.adapter.send.assert_not_awaited()
    r.bot.send_message.assert_not_awaited()
    r.bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_exhaustion_defers_cancelled_goal_before_absent_finalization(runtime, monkeypatch, caplog):
    r = runtime
    r.state["phase"] = "pending"
    r.db.set_meta(f"goal:{r.entry.session_id}", json.dumps({"status": "cancelled", "goal": "Cancelled goal"}))
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=("tasks",)))
    failed = False

    async def cli(_runner, _source, *args, **kwargs):
        nonlocal failed
        if args[0] == "tasks" and not failed:
            failed = True
            raise RuntimeError("tasks: EMAXCONNSESSION: connection limit")
        return []

    monkeypatch.setattr(work, "cli", AsyncMock(side_effect=cli))
    retries = []
    monkeypatch.setattr(retire, "_schedule_retry", lambda _runner, _source, delay: retries.append(delay))
    await retire.save(r.runner, r.source, r.state)

    with caplog.at_level("INFO", logger=retire.logger.name):
        await retire.advance(r.runner, r.source)

    pending = await retire.load(r.runner, r.source)
    assert pending["phase"] == "pending" and pending["transient_retries"] == 1
    assert retries == [30]
    assert "transient error" in caplog.text
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is not None
    assert r.runner.session_store.peek_session_id(r.entry.session_key) == r.entry.session_id

    await retire.advance(r.runner, r.source)
    final = await retire.load(r.runner, r.source)
    assert final["phase"] == "done"
    assert failed
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is None
    assert r.runner.session_store.peek_session_id(r.entry.session_key) is None
    assert json.loads(r.db.get_meta(f"goal:{r.entry.session_id}"))["status"] == "cancelled"

    await retire.advance(r.runner, r.source)
    assert await retire.load(r.runner, r.source) == final


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("present", [False, True])
async def test_real_inventory_cancels_active_control_on_topic_retirement(runtime, monkeypatch, kind, present, caplog):
    r = runtime
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager
    manager = GoalManager if kind == "goal" else LoopManager
    clear = manager.clear
    cleared = []
    def observed_clear(self):
        cleared.append(self)
        return clear(self)
    monkeypatch.setattr(manager, "clear", observed_clear)
    r.state.update(phase="pending", reason="User explicitly stopped work.")
    if present:
        from gateway.platforms.base import SendResult
        r.adapter.send.return_value = SendResult(success=True, message_id="19",
                                                 raw_response={"requested_thread_id": "17"})
        deleted = False

        async def delete(**kwargs):
            nonlocal deleted
            deleted = True
            return True

        async def cli(*args, **kwargs):
            return [] if deleted else [{"id": "17"}]

        r.bot.delete_forum_topic.side_effect = delete
        monkeypatch.setattr(work, "cli", AsyncMock(side_effect=cli))
    if kind == "goal":
        from hermes_cli import goals
        goals.save_goal(r.entry.session_id, goals.GoalState(goal="Retire this work", status="active"))
    else:
        from hermes_cli import loops
        loops.save_loop(r.entry.session_id, loops.LoopState(prompt="Retire this loop", status="active"))
    r.db.create_session("unrelated", source="telegram")
    unrelated = {"goal": "Keep working elsewhere", "status": "active"}
    r.db.set_meta("goal:unrelated", json.dumps(unrelated))
    await retire.save(r.runner, r.source, r.state)

    with caplog.at_level("INFO", logger=work.__name__):
        await retire.advance(r.runner, r.source)
        if present:
            prepared = await retire.load(r.runner, r.source)
            assert prepared is not None
            assert prepared["phase"] == "waiting"
            assert [item["id"] for item in prepared["preparation"]["items"]] == [kind]
            assert json.loads(r.db.get_meta(f"{kind}:{r.entry.session_id}"))["status"] == "active"
            assert not cleared
            event = r.adapter.handle_message.await_args.args[0]
            assert kind in event.text and "Choosing delete clears unowned source goals and loops" in event.text
            # Merely observing a queued wake is not a model owner decision.
            await retire.advance(r.runner, r.source)
            assert not cleared
            assert r.adapter.handle_message.await_count == 1
            await retire.request(r.runner, r.source, generation=prepared["generation"])
            await retire.advance(r.runner, r.source)

    final = await retire.load(r.runner, r.source)
    assert final is not None and final["phase"] == "done"
    if present:
        assert final["closing_receipt"]["message_id"] == "19"
        r.bot.delete_forum_topic.assert_awaited_once()
    record = json.loads(r.db.get_meta(f"{kind}:{r.entry.session_id}"))
    assert record["status"] == "cleared"
    assert len(cleared) == 1
    cancellation, = final["cancelled_controls"].values()
    assert cancellation["kind"] == kind and cancellation["action"] == "clear"
    assert f"{kind}:{cancellation['control_id']}" in final["cancelled_controls"]
    assert f"cleared {kind}" in caplog.text
    assert json.loads(r.db.get_meta("goal:unrelated")) == unrelated
    assert await work.inventory(r.runner, r.source, final) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
async def test_preparation_owner_choice_preserves_active_control_without_cancellation(runtime, monkeypatch, kind):
    r = runtime
    from hermes_cli import goals, loops
    manager = goals.GoalManager if kind == "goal" else loops.LoopManager
    monkeypatch.setattr(manager, "clear", lambda _: pytest.fail("Owner choice must transfer, not cancel"))
    record = {"status": "active", "goal" if kind == "goal" else "prompt": "Keep the standing commitment"}
    r.db.set_meta(f"{kind}:{r.entry.session_id}", json.dumps(record))
    r.state.update(phase="pending", reason="The topic is finished.")
    await retire.save(r.runner, r.source, r.state)
    assert not await work.prepare(r.runner, r.source, r.state)
    assert [item["id"] for item in r.state["preparation"]["items"]] == [kind]
    assert json.loads(r.db.get_meta(f"{kind}:{r.entry.session_id}")) == record

    r.db.create_session("next", source="telegram")
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18", "title": "Service Operations"}
    monkeypatch.setattr(work, "destination", AsyncMock(return_value=dest))
    monkeypatch.setattr(work, "receipt", AsyncMock(return_value={"receipt_id": "accepted"}))
    await retire.request(r.runner, r.source, generation=r.state["generation"], owners={kind: dest["title"]})
    decided = await retire.load(r.runner, r.source)
    assert decided is not None
    assert await work.prepare(r.runner, r.source, decided)
    successor = json.loads(r.db.get_meta(f"{kind}:next"))
    assert successor["status"] == "active"
    assert successor["goal" if kind == "goal" else "prompt"] == "Keep the standing commitment"
    assert decided["handoffs"][kind]["moved"]
    assert not decided.get("cancelled_controls")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("fence", ["running", "rebound", "generation", "phase"])
async def test_absent_finalization_fences_before_clearing_real_control(runtime, monkeypatch, kind, fence):
    r = runtime
    record = {"status": "active", "goal" if kind == "goal" else "prompt": "Must survive a declined pass"}
    r.db.set_meta(f"{kind}:{r.entry.session_id}", json.dumps(record))
    r.state.update(phase="pending", reason="Topic deletion selected.")
    await retire.save(r.runner, r.source, r.state)

    async def probe(*args, **kwargs):
        # The fence changes after the initial binding check but before finalization.
        if fence == "running":
            r.runner._is_session_running = lambda _: True
        elif fence == "rebound":
            r.runner.session_store.get_or_create_session(r.source, force_new=True)
        else:
            newer = {**r.state, "generation": "new"} if fence == "generation" else {**r.state, "phase": "cancelled"}
            await retire.save(r.runner, r.source, newer, force=True)
        return []

    monkeypatch.setattr(work, "cli", AsyncMock(side_effect=probe))
    assert await retire._finalize_absent(r.runner, r.source, r.state, r.bot)
    assert json.loads(r.db.get_meta(f"{kind}:{r.entry.session_id}")) == record
    final = await retire.load(r.runner, r.source)
    assert final is not None and not final.get("cancelled_controls")
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is not None
    r.bot.delete_forum_topic.assert_not_awaited()
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
async def test_control_replacement_preserves_each_cancellation_receipt(runtime, kind):
    r = runtime
    from hermes_cli import goals, loops
    r.state.update(phase="pending", reason="Topic deletion selected.")
    await retire.save(r.runner, r.source, r.state)
    previous = {}
    for title in ("Original work", "Replacement work"):
        if kind == "goal":
            goals.save_goal(r.entry.session_id, goals.GoalState(goal=title))
        else:
            loops.save_loop(r.entry.session_id, loops.LoopState(prompt=title))
        before = r.db.get_meta(f"{kind}:{r.entry.session_id}")
        control_id = json.loads(before)["mutation_id"]
        items = await work.inventory(r.runner, r.source, r.state)
        assert not await work.reconcile_controls(r.runner, r.source, r.state, items, confirmed_absent=True)
        stored = await retire.load(r.runner, r.source)
        assert stored is not None
        receipts = stored["cancelled_controls"]
        assert receipts[f"{kind}:{control_id}"]["before"] == before
        assert all(receipts[key] == value for key, value in previous.items())
        assert len(receipts) == len(previous) + 1
        previous = dict(receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_kind", ["goal", "loop"])
async def test_new_control_blocks_all_clears_in_the_same_transaction(runtime, monkeypatch, new_kind):
    r = runtime
    from hermes_cli import goals, loops
    for manager in (goals.GoalManager, loops.LoopManager):
        monkeypatch.setattr(manager, "clear", lambda _: pytest.fail("All blockers must be checked before clearing"))
    kind = "loop" if new_kind == "goal" else "goal"
    record = {"status": "active", "goal" if kind == "goal" else "prompt": "Original control"}
    r.db.set_meta(f"{kind}:{r.entry.session_id}", json.dumps(record))
    r.state.update(phase="pending", reason="Topic deletion selected.")
    await retire.save(r.runner, r.source, r.state)
    items = await work.inventory(r.runner, r.source, r.state)
    new_record = {"status": "active", "goal" if new_kind == "goal" else "prompt": "New undecided control"}
    r.db.set_meta(f"{new_kind}:{r.entry.session_id}", json.dumps(new_record))
    with pytest.raises(work.WorkInProgress, match="new source control"):
        await work.reconcile_controls(r.runner, r.source, r.state, items, confirmed_absent=True)
    assert json.loads(r.db.get_meta(f"{kind}:{r.entry.session_id}")) == record
    assert json.loads(r.db.get_meta(f"{new_kind}:{r.entry.session_id}")) == new_record
    stored = await retire.load(r.runner, r.source)
    assert stored is not None and not stored.get("cancelled_controls")


@pytest.mark.asyncio
async def test_real_inventory_treats_cancelled_goal_as_terminal(runtime):
    r = runtime
    r.db.set_meta(f"goal:{r.entry.session_id}", json.dumps({"status": "cancelled", "goal": "Already stopped"}))
    assert await work.inventory(r.runner, r.source, r.state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("path", ["absent", "prepare"])
async def test_real_inbox_blocks_control_clear_until_work_settles(runtime, monkeypatch, kind, path):
    r = runtime
    record = {"status": "active", "goal" if kind == "goal" else "prompt": "Keep working until safe to retire"}
    r.db.set_meta(f"{kind}:{r.entry.session_id}", json.dumps(record))
    r.state.update(phase="pending", reason="Topic deletion selected.", prepared_controls={kind: record})
    await retire.save(r.runner, r.source, r.state)
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=("tasks",)))
    settled, inbox_reads = False, 0

    async def cli(_runner, _source, *args, **kwargs):
        nonlocal inbox_reads
        if args[0] != "tasks" or args[2] != "inbox":
            return []
        inbox_reads += 1
        # In prepare(), work can appear after the initial owner-selection pass.
        if not settled and (path == "absent" or inbox_reads > 1):
            return [{"id": "task1", "title": "Unsettled inbox work", "status": "waiting"}]
        return []

    monkeypatch.setattr(work, "cli", cli)
    if path == "absent":
        await retire.advance(r.runner, r.source)
    else:
        with pytest.raises(RuntimeError, match="Open work still points"):
            await work.prepare(r.runner, r.source, r.state)
    pending = await retire.load(r.runner, r.source)
    assert pending is not None and pending["phase"] == "pending" and not pending.get("cancelled_controls")
    assert json.loads(r.db.get_meta(f"{kind}:{r.entry.session_id}")) == record
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is not None
    assert r.runner.session_store.peek_session_id(r.entry.session_key) == r.entry.session_id
    r.adapter.send.assert_not_awaited()
    r.bot.delete_forum_topic.assert_not_awaited()

    settled = True
    if path == "prepare":
        assert await work.prepare(r.runner, r.source, pending)
    await retire.advance(r.runner, r.source)
    final = await retire.load(r.runner, r.source)
    assert final is not None and final["phase"] == "done" and final["cancelled_controls"]
    assert json.loads(r.db.get_meta(f"{kind}:{r.entry.session_id}"))["status"] == "cleared"
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is None
    assert r.runner.session_store.peek_session_id(r.entry.session_key) is None


@pytest.mark.asyncio
async def test_real_inventory_keeps_running_process_as_blocking_work(runtime, monkeypatch):
    r = runtime
    from tools.process_registry import ProcessRegistry, ProcessSession, process_registry
    registry = ProcessRegistry()
    registry._running = {"proc-live": ProcessSession(id="proc-live", command="build", owner_task_id="old",
                                                       session_key=r.runner._session_key_for_source(r.source))}
    monkeypatch.setattr(process_registry, "list_sessions", registry.list_sessions)
    items = await work.inventory(r.runner, r.source, r.state)
    assert {item["id"] for item in items} == {"process:proc-live"}
    await retire.save(r.runner, r.source, r.state)
    await retire.advance(r.runner, r.source)
    assert (await retire.load(r.runner, r.source))["phase"] != "done"
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is not None
    r.bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,field,value", [
    ("goal", "max_turns", 3), ("loop", "until", "The service is healthy"),
    ("cron", "prompt", "Watch a different service"),
])
async def test_real_definition_change_rewakes_preparation_without_title_change(runtime, monkeypatch, kind, field, value):
    r = runtime
    from cron import jobs
    record = {"status": "active", "goal" if kind == "goal" else "prompt": "Watch Service",
              "gates": [{"command": "true", "attempts": 0}]}
    if kind == "cron":
        record.update(id="watch", name="Watch Service", deliver="telegram:101:17")
        monkeypatch.setattr(jobs, "list_jobs", lambda **_: [dict(record)])

    async def prepare():
        if kind != "cron":
            r.db.set_meta(f"{kind}:{r.entry.session_id}", json.dumps(record))
        state = await retire.load(r.runner, r.source)
        assert not await work.prepare(r.runner, r.source, state)
        return await retire.load(r.runner, r.source)

    r.state["phase"] = "pending"
    await retire.save(r.runner, r.source, r.state)
    first = await prepare()
    assert first is not None and r.adapter.handle_message.await_count == 1
    record["gates"][0]["attempts"] = 1
    record.update(turns_used=2, ticks_fired=2, next_run=24, last_output_tail="New runtime output")
    await prepare()
    assert r.adapter.handle_message.await_count == 1
    record[field] = value
    changed = await prepare()
    assert changed is not None and r.adapter.handle_message.await_count == 2
    assert changed["preparation"]["items"][0]["title"] == first["preparation"]["items"][0]["title"]
    assert changed["preparation"]["items"][0]["record"][field] == value
    await prepare()
    assert r.adapter.handle_message.await_count == 2
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_inventory_claim_blocks_cancellation_and_finalization(runtime, monkeypatch):
    r = runtime
    from hermes_cli import goals
    goals.save_goal(r.entry.session_id, goals.GoalState(goal="Pending cancelled work", status="active"))
    r.state.update(phase="pending", reason="User stopped work.")
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=("tasks",)))

    async def cli(_runner, _source, *args, **kwargs):
        return [{"id": "claim1"}] if args[0] == "tasks" else []

    monkeypatch.setattr(work, "cli", AsyncMock(side_effect=cli))
    await retire.save(r.runner, r.source, r.state)
    with pytest.raises(work.WorkInProgress):
        await work.inventory(r.runner, r.source, r.state)
    await retire.advance(r.runner, r.source)
    assert (await retire.load(r.runner, r.source))["phase"] == "pending"
    assert json.loads(r.db.get_meta(f"goal:{r.entry.session_id}"))["status"] == "active"
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is not None


@pytest.mark.asyncio
async def test_control_cancellation_does_not_discard_inflight_loop_tick(runtime):
    r = runtime
    from hermes_cli import loops
    loops.save_loop(r.entry.session_id, loops.LoopState(prompt="Still executing", status="active",
                                                       awaiting_response=True, ticks_fired=1))
    r.state.update(phase="pending", reason="User stopped work.")
    await retire.save(r.runner, r.source, r.state)
    items = await work.inventory(r.runner, r.source, r.state)
    with pytest.raises(work.WorkInProgress):
        await work.reconcile_controls(r.runner, r.source, r.state, items, confirmed_absent=True)
    record = json.loads(r.db.get_meta(f"loop:{r.entry.session_id}"))
    assert record["status"] == "active" and record["awaiting_response"]
    assert "cancelled_controls" not in (await retire.load(r.runner, r.source))


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["inventory", "merge", "handoff"])
async def test_waiting_absent_topic_keeps_unsettled_work(runtime, monkeypatch, block):
    r = runtime
    r.state["phase"] = "waiting"
    prepare = AsyncMock(return_value=False)
    monkeypatch.setattr(work, "prepare", prepare)
    if block == "inventory":
        monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[{"id": "goal", "kind": "goal"}]))
    elif block == "merge":
        r.state["target"] = "Successor"
    else:
        monkeypatch.setattr(work, "settle_handoffs", AsyncMock(side_effect=work.HandoffPending("Not delivered")))
    await retire.save(r.runner, r.source, r.state)

    await retire.advance(r.runner, r.source)

    assert await retire.load(r.runner, r.source) == r.state
    assert r.db.get_telegram_topic_binding(chat_id="101", thread_id="17") is not None
    assert r.runner.session_store.peek_session_id(r.entry.session_key) == r.entry.session_id
    prepare.assert_not_awaited()
    r.adapter.send.assert_not_awaited()
    r.bot.send_message.assert_not_awaited()
    r.bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["present", "outage"])
async def test_waiting_unconfirmed_absence_still_prepares(runtime, monkeypatch, probe):
    r = runtime
    r.state["phase"] = "waiting"
    lookup = AsyncMock(return_value=[{"id": "17"}])
    if probe == "outage":
        lookup.side_effect = RuntimeError("network outage")
    monkeypatch.setattr(work, "cli", lookup)
    prepare = AsyncMock(return_value=False)
    monkeypatch.setattr(work, "prepare", prepare)
    await retire.save(r.runner, r.source, r.state)

    await retire.advance(r.runner, r.source)

    prepare.assert_awaited_once_with(r.runner, r.source, r.state)
    assert await retire.load(r.runner, r.source) == r.state
    assert r.runner.session_store.peek_session_id(r.entry.session_key) == r.entry.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", ["closing_attempt", "closing_receipt"])
async def test_waiting_closing_evidence_skips_passive_finalization(runtime, monkeypatch, closing):
    r = runtime
    r.state.update(phase="waiting", **{closing: {"message_id": "10"} if closing == "closing_receipt" else 1.0})
    prepare = AsyncMock(return_value=False)
    monkeypatch.setattr(work, "prepare", prepare)
    await retire.save(r.runner, r.source, r.state)

    await retire.advance(r.runner, r.source)

    work.cli.assert_not_awaited()
    prepare.assert_awaited_once_with(r.runner, r.source, r.state)
    assert await retire.load(r.runner, r.source) == r.state
    assert r.runner.session_store.peek_session_id(r.entry.session_key) == r.entry.session_id
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


@pytest.mark.asyncio
async def test_failed_retirement_of_a_replaced_conversation_is_not_retried(runtime, monkeypatch):
    """Auto-retry retires only the conversation the user asked to retire, never a /new successor."""
    r = runtime
    r.state["failed_at"] = 1.0
    monkeypatch.setattr(work, "cli", AsyncMock(return_value=[{"id": "17"}]))
    r.runner.session_store.get_or_create_session(r.source, force_new=True)
    await retire.save(r.runner, r.source, r.state)
    await retire.resume_all(r.runner)
    state = await retire.load(r.runner, r.source)
    assert state["phase"] == "failed" and state["generation"] == "gen" and not state.get("auto_retries")
