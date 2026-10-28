"""Retirement discovers every native owner and moves authoritative routes, not just briefs."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.platforms.base import SendResult
from gateway import telegram_retire as retire
from gateway import telegram_retire_work as work
from hermes_state import AsyncSessionDB, SessionDB


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="101", thread_id="17", chat_type="dm", user_id="101")
    db.create_session("old", source="telegram")
    meta = AsyncSessionDB(db)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True, message_id="19",
                                                                    raw_response={"requested_thread_id": "17"})))
    runner = SimpleNamespace(_session_db=meta, _session_key_for_source=lambda s: "old-key" if s.thread_id == "17" else "next-key",
                             _delivery_adapter_for=lambda _: adapter,
                             session_store=SimpleNamespace(peek_session_id=lambda key: "old" if key == "old-key" else "next"))
    state = {"generation": "gen", "phase": "pending", "session_id": "old", "session_key": "old-key",
             "target": "", "handoffs": {}, "reason": "Current work is finished."}
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=()))
    from cron import jobs
    import tools.async_delegation as delegation
    from tools.process_registry import process_registry
    monkeypatch.setattr(jobs, "list_jobs", lambda **_: [])
    monkeypatch.setattr(delegation, "retirement_inventory", lambda *_: [])
    monkeypatch.setattr(process_registry, "list_sessions", lambda **_: [])
    yield SimpleNamespace(runner=runner, source=source, state=state, db=db, adapter=adapter)
    db.close()


@pytest.mark.asyncio
async def test_inventory_exact_routes_all_native_stores(runtime, monkeypatch):
    r = runtime
    from cron import jobs
    import tools.async_delegation as delegation
    from tools.process_registry import process_registry
    r.db.set_meta("goal:old", json.dumps({"status": "active", "goal": "Maintain Service"}))
    r.db.set_meta("loop:old", json.dumps({"status": "paused", "prompt": "Watch Service"}))
    monkeypatch.setattr(jobs, "list_jobs", lambda **_: [
        {"id": "owned", "enabled": True, "deliver": "telegram:101:17"},
        {"id": "list", "enabled": False, "deliver": ["telegram:101:17", "telegram:101:19"]},
        {"id": "prefix", "deliver": "telegram:101:170"},
        {"id": "attach", "attach_to_session": "old"},
        {"id": "origin", "origin": {"platform": "telegram", "chat_id": "101", "thread_id": "17"}},
    ])
    # Use the real registry filter: list_sessions(session_key=...) never returns
    # another topic, and its public records intentionally omit session_key.
    from tools.process_registry import ProcessRegistry, ProcessSession
    registry = ProcessRegistry()
    registry._running = {
        "proc1": ProcessSession(id="proc1", command="build", owner_task_id="old", session_key="old-key"),
        "proc2": ProcessSession(id="proc2", command="unrelated", owner_task_id="other", session_key="other-key"),
    }
    monkeypatch.setattr(process_registry, "list_sessions", registry.list_sessions)
    monkeypatch.setattr(delegation, "retirement_inventory", lambda *_: [{"delegation_id": "d1", "goal": "Build Service"}])
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=("tasks",)))
    command = AsyncMock(side_effect=[[], [{"id": "t1", "status": "waiting", "title": "Service Delivery"}]])
    monkeypatch.setattr(work, "cli", command)
    items = await work.inventory(r.runner, r.source, r.state)
    assert {item["id"] for item in items} == {
        "goal", "loop", "cron:owned", "cron:list", "cron:attach", "cron:origin", "process:proc1", "delegation:d1", "task:t1"}
    assert command.await_args_list[0].args[-2:] == ("--session", "hermes:default/old")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected,error", [
    (b'[]', [], None),
    (b'[{"id":"t1"}]', [{"id": "t1"}], None),
    (b'{"data":[{"id":"t1"}]}', [{"id": "t1"}], None),
    (b'{"id":"r1","status":"delivered"}', {"id": "r1", "status": "delivered"}, None),
    (b'{"error":{"message":"unavailable"}}', None, "unavailable"),
])
async def test_cli_parses_raw_subprocess_json(runtime, monkeypatch, tmp_path, raw, expected, error):
    from tools.environments import local
    r = runtime
    r.runner._resolve_profile_home_for_source = lambda _: tmp_path
    monkeypatch.setattr(local, "served_profile_child_env", lambda **_: {"HERMES_HOME": str(tmp_path)})
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=("/fake/tasks",)))
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(raw, b"")))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(work.asyncio, "create_subprocess_exec", spawn)
    if error:
        with pytest.raises(RuntimeError, match=error):
            await work.cli(r.runner, r.source, "tasks", "--json", "inbox")
    else:
        assert await work.cli(r.runner, r.source, "tasks", "--json", "inbox") == expected
    assert spawn.await_args.args == ("/fake/tasks", "--json", "inbox")
    assert spawn.await_args.kwargs["env"]["HERMES_SESSION_ID"] == "old"
    assert spawn.await_args.kwargs["env"]["HERMES_HOME"] == str(tmp_path)


@pytest.mark.asyncio
async def test_done_goal_not_open_work(runtime):
    runtime.db.set_meta("goal:old", json.dumps({"status": "done", "goal": "Finished"}))
    assert await work.inventory(runtime.runner, runtime.source, runtime.state) == []


@pytest.mark.asyncio
async def test_active_claim_blocks_and_partial_task_inventory_fails_closed(runtime, monkeypatch):
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=("tasks",)))
    monkeypatch.setattr(work, "cli", AsyncMock(return_value=[{"id": "claim1"}]))
    with pytest.raises(RuntimeError, match="Active task execution"):
        await work.inventory(runtime.runner, runtime.source, runtime.state)
    monkeypatch.setattr(work, "cli", AsyncMock(side_effect=[[], {"tasks": []}]))
    with pytest.raises(RuntimeError, match="incomplete response"):
        await work.inventory(runtime.runner, runtime.source, runtime.state)


@pytest.mark.asyncio
async def test_obvious_owner_moves_and_unknown_owner_gets_one_internal_preparation(runtime, monkeypatch):
    r = runtime
    r.state["owners"] = {"goal": "Service Operations"}
    known = {"id": "goal", "kind": "goal", "title": "Service Operations"}
    unknown = {"id": "task:x", "kind": "task", "title": "Unclear work"}
    remaining = [known, unknown]
    monkeypatch.setattr(work, "inventory", AsyncMock(side_effect=lambda *_: list(remaining)))
    async def destination(*args):
        if args[-1] == known["title"]:
            return {"session_id": "next", "session_key": "next-key", "thread_id": "18"}
        raise ValueError("unclear")
    monkeypatch.setattr(work, "destination", destination)
    async def admit(event):
        event._gateway_accepted = True
    r.adapter.handle_message = AsyncMock(side_effect=admit)
    sent = AsyncMock(return_value={"receipt_id": "r1", "address": "hermes:default/next"})
    monkeypatch.setattr(work, "receipt", sent)
    async def transfer(*args):
        remaining.remove(args[-2])
    monkeypatch.setattr(work, "transfer", transfer)
    assert not await work.prepare(r.runner, r.source, r.state)
    assert r.state["handoffs"]["goal"]["moved"]
    r.adapter.send.assert_not_awaited()
    event = r.adapter.handle_message.await_args.args[0]
    assert event.internal and event.reply_expected is False
    assert event.metadata["retirement_generation"] == "gen"
    assert "task:x" in event.text
    assert not await work.prepare(r.runner, r.source, r.state)
    assert r.adapter.handle_message.await_count == 1
    assert sent.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["process", "cron"])
async def test_unchanged_live_work_does_not_repeat_preparation(runtime, monkeypatch, kind):
    from cron import jobs
    from tools.process_registry import process_registry

    r = runtime
    samples = iter(range(3))

    def live_records(**_):
        sample = next(samples)
        if kind == "process":
            return [{"session_id": "worker", "command": "watch service", "status": "running",
                     "owner_task_id": "old", "uptime_seconds": sample,
                     "output_preview": f"Health check {sample}"}]
        return [{"id": "watch", "name": "Service Watch", "enabled": True,
                 "deliver": "telegram:101:17", "last_run": sample, "next_run": sample + 1}]

    if kind == "process":
        monkeypatch.setattr(process_registry, "list_sessions", live_records)
    else:
        monkeypatch.setattr(jobs, "list_jobs", live_records)

    async def admit(event):
        event._gateway_accepted = True

    r.adapter.handle_message = AsyncMock(side_effect=admit)
    await retire.save(r.runner, r.source, r.state)
    for _ in range(3):
        state = await retire.load(r.runner, r.source)
        assert state is not None
        assert not await work.prepare(r.runner, r.source, state)
    assert r.adapter.handle_message.await_count == 1
    waiting = await retire.load(r.runner, r.source)
    assert waiting is not None and waiting["phase"] == "waiting"
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_handoff_reconciles_even_when_item_no_longer_in_inventory(runtime, monkeypatch):
    r = runtime
    r.state["handoffs"] = {"task:t1": {"item": {"id": "task:t1", "kind": "task", "task_id": "t1"},
                                           "destination": {"session_id": "next"}, "moved": False}}
    monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[]))
    receipt = AsyncMock(side_effect=[RuntimeError("not delivered"), {"receipt_id": "r1"}])
    transfer = AsyncMock()
    monkeypatch.setattr(work, "receipt", receipt)
    monkeypatch.setattr(work, "transfer", transfer)
    with pytest.raises(RuntimeError, match="not delivered"):
        await work.prepare(r.runner, r.source, r.state)
    transfer.assert_not_awaited()
    assert await work.prepare(r.runner, r.source, r.state)
    transfer.assert_awaited_once()
    assert r.state["handoffs"]["task:t1"]["moved"]


@pytest.mark.asyncio
async def test_merge_hands_context_over_even_without_open_items_and_reuses_its_receipt(runtime, monkeypatch):
    r = runtime
    r.state["target"] = "Service Operations"
    r.state["context"] = "The service is live. Preserve its standing uptime limit and next maintenance action."
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "19", "title": "Service Operations"}
    monkeypatch.setattr(work, "destination", AsyncMock(return_value=dest))
    monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[]))
    sent = AsyncMock(return_value={"receipt_id": "context-proof"})
    moved = AsyncMock()
    monkeypatch.setattr(work, "receipt", sent)
    monkeypatch.setattr(work, "transfer", moved)
    assert await work.prepare(r.runner, r.source, r.state)
    assert r.state["merge_destination"] == dest
    context = sent.await_args.args[3]
    assert context["kind"] == "context" and context["record"]["source_session"] == "old"
    assert context["record"]["reason"] == r.state["reason"]
    assert context["record"]["summary"] == r.state["context"]
    assert await work.prepare(r.runner, r.source, r.state)
    sent.assert_awaited_once()
    moved.assert_awaited_once()
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["posted", "pending", "sent_unconfirmed"])
@pytest.mark.parametrize("outcome", ["delivered", "replied", "misrouted", "wrong_target", "wrong_id", "error"])
async def test_pending_retirement_rechecks_same_receipt_on_resume(runtime, monkeypatch, pending, outcome):
    r = runtime
    r.runner._is_session_running = lambda _: False
    r.runner.session_store.unbind_session = MagicMock(return_value=True)
    r.adapter._bot = SimpleNamespace(delete_forum_topic=AsyncMock(return_value=True))
    r.adapter._session_tasks = {}

    @asynccontextmanager
    async def scope(_source):
        yield

    r.runner._async_profile_scope_for_source = scope
    monkeypatch.setattr(retire, "_probe_deleted", AsyncMock(return_value=True))
    monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[]))
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18", "title": "Service Operations"}
    monkeypatch.setattr(work, "destination", AsyncMock(return_value=dest))
    reads, sends = [], []
    reply = {"id": "r1", "target": "hermes:default/next", "status": pending}

    async def command(_runner, _source, *args, **kwargs):
        if "send" in args:
            sends.append(args)
            return {"id": "r1"}
        assert args == ("relay", "--json", "status", "--wait", f"{work.RELAY_CONFIRM_WAIT}s", "r1")
        assert kwargs["timeout"] == work.RELAY_CONFIRM_WAIT + 30
        reads.append(args)
        return dict(reply)

    monkeypatch.setattr(work, "cli", command)
    await retire.request(r.runner, r.source, target=dest["title"], reason="Merge service ownership.",
                         context="Keep the existing uptime limit.")
    await retire.advance(r.runner, r.source)
    state = await retire.load(r.runner, r.source)
    assert state["phase"] == "pending"
    assert state["handoffs"]["context:next"]["receipt_id"] == "r1"
    assert not state["handoffs"]["context:next"]["moved"]
    r.adapter.send.assert_not_awaited()
    r.adapter._bot.delete_forum_topic.assert_not_awaited()

    # A later housekeeping tick and an explicit retry recover from stored state,
    # never by issuing another non-idempotent send to the busy target.
    await retire.resume_all(r.runner)
    retry = await retire.request(r.runner, r.source)
    assert retry["generation"] == state["generation"]
    assert (await retire.load(r.runner, r.source))["phase"] == "pending"
    r.adapter.send.assert_not_awaited()
    reply["status"] = outcome
    if outcome == "wrong_target":
        reply.update(status="delivered", target="hermes:default/other")
    elif outcome == "wrong_id":
        reply.update(status="delivered", id="other")
    elif outcome == "error":
        reply.update(status="delivered", error="readback failed")
    await retire.resume_all(r.runner)
    state = await retire.load(r.runner, r.source)
    assert len(sends) == 1 and len(reads) == 3
    if outcome in {"delivered", "replied"}:
        assert state["phase"] == "done"
        assert state["handoffs"]["context:next"]["moved"]
        r.adapter._bot.delete_forum_topic.assert_awaited_once()
    else:
        assert state["phase"] == "failed"
        assert not state["handoffs"]["context:next"]["moved"]
        r.adapter._bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_settlement_carries_the_summary_without_inventing_a_ledger_obligation(runtime, monkeypatch):
    from pathlib import Path
    r = runtime
    summary = "Service is live. The uptime limit and maintenance owner remain unchanged."
    item = {"id": "context:next", "kind": "context", "record": {"source_session": "old", "summary": summary}}
    dest = {"session_id": "next", "thread_id": "19"}
    async def cli(*args, **kwargs):
        command = args[2:]
        assert command[0] == "relay"
        if "send" in command:
            assert "--fyi" in command and "--task" not in command
            body = Path(command[command.index("--body-file") + 1]).read_text()
            assert summary in body and '"source_session": "old"' in body
            return {"id": "context-proof"}
        assert "status" in command
        return {"id": "context-proof", "target": "hermes:default/next", "status": "delivered"}
    mocked = AsyncMock(side_effect=cli)
    monkeypatch.setattr(work, "cli", mocked)
    result = await work.receipt(r.runner, r.source, r.state, item, dest)
    assert result["receipt_id"] == "context-proof" and mocked.await_count == 2


@pytest.mark.asyncio
async def test_active_claims_are_quiet_and_destination_controls_are_not_overwritten(runtime, monkeypatch):
    r = runtime
    async def admit(event):
        event._gateway_accepted = True
    r.adapter.handle_message = AsyncMock(side_effect=admit)
    monkeypatch.setattr(work, "inventory", AsyncMock(side_effect=work.WorkInProgress("Active claim")))
    assert not await work.prepare(r.runner, r.source, r.state)
    assert "Active claim" in r.adapter.handle_message.await_args.args[0].text
    r.adapter.send.assert_not_awaited()
    record = {"goal": "Maintain Service", "status": "active"}
    r.db.set_meta("goal:next", json.dumps(record))
    r.state["owners"] = {"goal": "Service Operations"}
    item = {"id": "goal", "kind": "goal", "title": "Maintain Service", "record": record}
    monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[item]))
    monkeypatch.setattr(work, "destination", AsyncMock(return_value={"session_id": "next", "title": "Service Operations"}))
    sent = AsyncMock()
    monkeypatch.setattr(work, "receipt", sent)
    assert not await work.prepare(r.runner, r.source, r.state)
    assert "Destination already has a control" in r.adapter.handle_message.await_args.args[0].text
    assert json.loads(r.db.get_meta("goal:next")) == record
    sent.assert_not_awaited()
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_preparation_records_owners_but_cannot_rearm_a_cancelled_generation(runtime):
    r = runtime
    await retire.save(r.runner, r.source, r.state)
    result = await retire.request(r.runner, r.source, owners={"goal": "Service Operations"},
                                 reason="Service Operations owns the continuing service.", generation="gen")
    state = await retire.load(r.runner, r.source)
    assert state["owners"] == {"goal": "Service Operations"}
    assert result["generation"] != "gen"
    await retire.request(r.runner, r.source, cancel=True)
    with pytest.raises(ValueError, match="superseded"):
        await retire.request(r.runner, r.source, target="Service Operations", reason="Old preparation",
                             generation=result["generation"])
    assert (await retire.load(r.runner, r.source))["phase"] == "cancelled"


@pytest.mark.asyncio
async def test_goal_and_loop_use_actual_persisted_migration(runtime, monkeypatch):
    from hermes_cli import goals, loops
    r = runtime
    r.db.create_session("next", source="telegram")
    monkeypatch.setattr(goals, "_get_session_db", lambda: r.db)
    monkeypatch.setattr(loops, "_get_session_db", lambda: r.db)
    goals.save_goal("old", goals.GoalState(goal="Maintain Service", subgoals=["Preserve history"], max_turns=7))
    loop = loops.LoopState(prompt="Watch Service")
    loops.save_loop("old", loop)
    items = await work.inventory(r.runner, r.source, r.state)
    for item in items:
        await work.transfer(r.runner, r.source, r.state, item, {"session_id": "next"})
        moved = json.loads(r.db.get_meta(f"{item['kind']}:next"))
        assert {k: v for k, v in moved.items() if k not in {"mutation_id", "control_generation"}} == {
            k: v for k, v in item["record"].items() if k not in {"mutation_id", "control_generation"}}
        # Reopen the durable state, with no in-memory movement acknowledgement.
        recovered = await retire.load(r.runner, r.source)
        assert not recovered["handoffs"][item["id"]]["moved"]
        await work.transfer(r.runner, r.source, recovered, item, {"session_id": "next"})
        assert json.loads(r.db.get_meta(f"{item['kind']}:next")) == moved
    assert goals.load_goal("old").status == "cleared"
    assert goals.load_goal("next").goal == "Maintain Service"
    assert loops.load_loop("old").status == "cleared"
    assert loops.load_loop("next").prompt == "Watch Service"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("failure", ["destination", "source", "receipt_write"])
async def test_control_transfer_refuses_conflicts_and_rolls_back_without_proof(runtime, monkeypatch, kind, failure):
    from hermes_cli import goals, loops
    r = runtime
    monkeypatch.setattr(goals, "_get_session_db", lambda: r.db)
    monkeypatch.setattr(loops, "_get_session_db", lambda: r.db)
    r.db.create_session("next", source="telegram")
    if kind == "goal":
        goals.save_goal("old", goals.GoalState(goal="Maintain Service", subgoals=["Do not lose work"]))
    else:
        loops.save_loop("old", loops.LoopState(prompt="Watch Service"))
    item = (await work.inventory(r.runner, r.source, r.state))[0]
    if failure == "destination":
        # Identical text is not evidence that an unrelated goal was moved.
        r.db.set_meta(f"{kind}:next", r.db.get_meta(f"{kind}:old"))
    elif failure == "source":
        changed = {**item["record"], "status": "paused"}
        r.db.set_meta(f"{kind}:old", json.dumps(changed))
    before = (r.db.get_meta(f"{kind}:old"), r.db.get_meta(f"{kind}:next"))
    if failure == "receipt_write":
        original = r.db.set_meta
        def fail(key, value, **kwargs):
            if key == retire.state_key(r.source):
                raise RuntimeError("proof write failed")
            return original(key, value, **kwargs)
        monkeypatch.setattr(r.db, "set_meta", fail)
    with pytest.raises(RuntimeError, match="Cannot transfer|proof write failed"):
        await work.transfer(r.runner, r.source, r.state, item, {"session_id": "next"})
    assert (r.db.get_meta(f"{kind}:old"), r.db.get_meta(f"{kind}:next")) == before
    assert r.db.get_meta(retire.state_key(r.source)) is None


@pytest.mark.asyncio
async def test_cron_moves_origin_delivery_failure_and_attach_with_exact_readback(runtime, monkeypatch):
    from cron import jobs
    r = runtime
    job = {"id": "cron1", "origin": {"chat_id": "101", "thread_id": "17", "platform": "telegram"},
           "deliver": ["telegram:101:17", "telegram:101:170"], "failure_deliver": "telegram:101:17",
           "attach_to_session": "old", "prompt": "Report to hermes:default/old"}
    def update(_, updates):
        job.update(updates)
    monkeypatch.setattr(jobs, "get_job", lambda _: dict(job))
    monkeypatch.setattr(jobs, "update_job", update)
    item = {"kind": "cron", "id": "cron:cron1", "record": {"id": "cron1"}}
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18"}
    await work.transfer(r.runner, r.source, r.state, item, dest)
    assert job["origin"]["thread_id"] == "18"
    assert job["deliver"] == ["telegram:101:18", "telegram:101:170"]
    assert job["failure_deliver"] == "telegram:101:18"
    assert job["attach_to_session"] == "next"
    assert job["prompt"] == "Report to hermes:default/next"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,target,receipt_id", [
    ("pending", "hermes:default/next", "r1"),
    ("delivered", "hermes:default/wrong", "r1"),
    ("delivered", "hermes:default/next", "wrong"),
])
async def test_receipt_requires_transcript_readback_before_moving(runtime, monkeypatch, status, target, receipt_id):
    r = runtime
    assignment = {"id": "a2", "assigned_to": "hermes:default/next",
                  "assigned_by": "hermes:default/parent", "report_to": "hermes:default/parent"}
    # relay status must reconcile that same receipt against its exact transcript.
    monkeypatch.setattr(work, "cli", AsyncMock(side_effect=[
        [assignment], {"id": "r1"}, {"id": receipt_id, "target": target, "status": status}]))
    with pytest.raises(RuntimeError, match="not confirmed"):
        await work.receipt(r.runner, r.source, r.state,
                           {"id": "task:t1", "kind": "task", "record": {
                               "id": "t1", "assignment_id": "a1", "assigned_by": "hermes:default/parent",
                               "report_to": "hermes:default/parent"}},
                           {"session_id": "next", "session_key": "next-key", "thread_id": "18"})


@pytest.mark.asyncio
async def test_receipt_accepts_actual_direct_relay_shape(runtime, monkeypatch):
    r = runtime
    target = "hermes:default/next"
    delivered = {"id": "r1", "target": target, "status": "delivered"}
    assignment = {"id": "a2", "assigned_to": target,
                  "assigned_by": "hermes:default/parent", "report_to": "hermes:default/parent"}
    command = AsyncMock(side_effect=[[assignment], delivered, delivered])
    monkeypatch.setattr(work, "cli", command)
    result = await work.receipt(r.runner, r.source, r.state,
                                {"id": "task:t1", "kind": "task", "record": {
                                    "id": "t1", "assignment_id": "a1", "assigned_by": "hermes:default/parent",
                                    "report_to": "hermes:default/parent"}},
                                {"session_id": "next", "session_key": "next-key", "thread_id": "18"})
    assert result["receipt_id"] == "r1" and result["address"] == target
    # A Hermes receipt is `posted` until the target stores it. Relay reconciles
    # for a bounded time and the subprocess deadline outlasts that wait.
    assert command.await_args.args[-4:] == ("status", "--wait", f"{work.RELAY_CONFIRM_WAIT}s", "r1")
    assert command.await_args.kwargs["timeout"] > work.RELAY_CONFIRM_WAIT


@pytest.mark.asyncio
async def test_cli_honors_a_longer_deadline_for_waiting_reads(runtime, monkeypatch, tmp_path):
    from tools.environments import local
    r = runtime
    r.runner._resolve_profile_home_for_source = lambda _: tmp_path
    monkeypatch.setattr(local, "served_profile_child_env", lambda **_: {})
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=("/fake/relay",)))
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'{"id":"r1"}', b"")))
    monkeypatch.setattr(work.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    deadlines = []
    real_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(work.asyncio, "wait_for", wait_for)
    await work.cli(r.runner, r.source, "relay", "--json", "status", "r1")
    await work.cli(r.runner, r.source, "relay", "--json", "status", "r1", timeout=90)
    assert deadlines == [30, 90]


@pytest.mark.asyncio
@pytest.mark.parametrize("original", ["hermes:default/parent", "codex:parent", "claude:parent", "brian:interactive", "hermes:default/old"])
async def test_task_handoff_preserves_reportback_and_reconciles_assignment(runtime, monkeypatch, original):
    import re

    r = runtime
    origin, target = "hermes:default/old", "hermes:default/next"
    record = {"id": "t1", "assignment_id": "a1", "assigned_by": original, "report_to": original}
    item = {"id": "task:t1", "kind": "task", "record": record}
    assignments = [{"id": "a1", "assigned_to": origin, "assigned_by": original,
                    "report_to": original, "accepted_at": None}]
    delivered = {"id": "r1", "target": target, "status": "delivered"}
    writes, keys = [], []

    async def command(_runner, source, *args, **_):
        if "assignments" in args:
            return [dict(a) for a in assignments]
        if "accept" in args:
            assignments[-1]["accepted_at"] = "accepted"
            return dict(assignments[-1])
        if "assign" in args:
            assert assignments[-1]["accepted_at"]
            actor = args[args.index("--agent-id") + 1]
            session = args[args.index("--session-id") + 1]
            by = actor + "/" + session if actor.startswith("hermes:") else actor + ":" + session
            report = args[args.index("--report-to") + 1]
            assert by == report == (target if original == origin else original)
            assignments.append({"id": "a2", "assigned_to": target, "assigned_by": by,
                                "report_to": report, "accepted_at": None})
            writes.append(tuple(args))
            return dict(assignments[-1])
        if "send" in args:
            key = args[args.index("--key") + 1]
            assert len(key) <= 200 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key)
            keys.append(key)
            return delivered
        if "status" in args:
            return delivered
        raise AssertionError(args)

    monkeypatch.setattr(work, "cli", command)
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18"}
    assert (await work.receipt(r.runner, r.source, r.state, item, dest))["receipt_id"] == "r1"
    assert (await work.receipt(r.runner, r.source, r.state, item, dest))["receipt_id"] == "r1"
    assert len(writes) == 1 and len(keys) == 1
    # This is the tasks ledger's reporting predicate, not just a target inbox lookup.
    assert assignments[-1]["assigned_by"] == assignments[-1]["report_to"]


@pytest.mark.asyncio
async def test_native_handoff_is_created_under_destination_not_reassigned_on_retry(runtime, monkeypatch):
    r = runtime
    delivered = {"id": "r1", "target": "hermes:default/next", "status": "delivered"}
    keys = []
    saved, assignments, sends = {}, [], []

    async def command(runner, source, *args, **_):
        if "query" in args:
            return [dict(saved)] if saved else []
        if "add" in args:
            sid = runner.session_store.peek_session_id(runner._session_key_for_source(source))
            assert work.address(source, sid) == "hermes:default/next"
            keys.append(args[args.index("--dedupe-key") + 1])
            saved.update(id="t1", title=args[3], context=args[args.index("--context") + 1],
                         dedupe_key=args[args.index("--dedupe-key") + 1],
                         source="agent_id=hermes:default;session_id=default/next")
            return dict(saved)
        if "show" in args:
            return dict(saved)
        if "assignments" in args:
            return list(assignments)
        if "assign" in args:
            by = args[args.index("--agent-id") + 1] + "/" + args[args.index("--session-id") + 1]
            assert by == args[args.index("--report-to") + 1] == "hermes:default/next"
            assignments.append({"id": "a1", "assigned_to": "hermes:default/next", "assigned_by": by, "report_to": by})
            return assignments[-1]
        if "send" in args:
            assert work.address(source, runner.session_store.peek_session_id(runner._session_key_for_source(source))) != args[3]
            sends.append(tuple(args))
            return delivered
        assert args[:3] == ("relay", "--json", "status")
        return delivered

    monkeypatch.setattr(work, "cli", command)
    item = {"id": "goal", "kind": "goal", "record": {}}
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18"}
    for _ in range(2):
        assert (await work.receipt(r.runner, r.source, r.state, item, dest))["receipt_id"] == "r1"
    assert len(keys) == 1
    assert len(assignments) == 1 and len(sends) == 1


@pytest.fixture
def loop_handoff_retry(runtime, monkeypatch):
    from hermes_cli.loops import LoopState
    r = runtime
    r.item = {"id": "loop", "kind": "loop", "title": "Watch service health",
              "record": json.loads(LoopState(prompt="Watch service health", interval_seconds=60,
                                              times=12, until="Service is stable.").to_json())}
    r.dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18"}
    r.task, r.creates, r.sends = {}, [], []
    r.corrupt_fresh = False
    r.assign = AsyncMock(side_effect=[RuntimeError("assignment interrupted"), None])
    monkeypatch.setattr(work, "assign_task", r.assign)

    async def command(_runner, _source, *args, **_):
        if "query" in args:
            assert args == ("tasks", "--json", "query", '"Retired topic: loop"', "--all")
            return [dict(r.task)] if r.task else []
        if "add" in args:
            if not r.task:
                r.task.update(id="t1", title=args[3], context=args[args.index("--context") + 1],
                              dedupe_key=args[args.index("--dedupe-key") + 1],
                              source="agent_id=hermes:default;session_id=default/next")
                r.creates.append(dict(r.task))
            return dict(r.task)
        if "show" in args:
            return {**r.task, "context": "truncated on write"} if r.corrupt_fresh else dict(r.task)
        if "send" in args:
            r.sends.append(args)
            return {"id": "r1"}
        assert args[:3] == ("relay", "--json", "status")
        return {"id": "r1", "target": "hermes:default/next", "status": "delivered"}

    monkeypatch.setattr(work, "cli", command)
    return r


@pytest.mark.asyncio
async def test_loop_handoff_dedupe_survives_new_tick_snapshot(loop_handoff_retry):
    r = loop_handoff_retry
    with pytest.raises(RuntimeError, match="assignment interrupted"):
        await work.receipt(r.runner, r.source, r.state, r.item, r.dest)
    original_context = r.task["context"]
    r.item["record"].update(ticks_fired=1, last_fired_at=10, next_due_at=70, run_generation=2,
                            tick_id="new-tick", mutation_id="new-write", current_delay=120,
                            awaiting_response=True, claim_phase="started", fired_version=1,
                            last_response_digest="changed", last_external_wake_tick=1, backoff_level=1,
                            control_generation=3)
    result = await work.receipt(r.runner, r.source, r.state, r.item, r.dest)
    assert result["receipt_id"] == "r1"
    assert r.task["context"] == original_context
    assert len(r.creates) == len(r.sends) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["prompt", "cadence", "until", "limit", "task_id", "dedupe_key", "title", "target", "fresh_context"])
async def test_loop_handoff_rejects_dedupe_identity_changes_and_truncated_fresh_context(loop_handoff_retry, change):
    r = loop_handoff_retry
    if change == "fresh_context":
        r.corrupt_fresh = True
    else:
        with pytest.raises(RuntimeError, match="assignment interrupted"):
            await work.receipt(r.runner, r.source, r.state, r.item, r.dest)
        if change in {"prompt", "cadence", "until", "limit"}:
            field = {"cadence": "interval_seconds", "limit": "times"}.get(change, change)
            r.item["record"][field] = "different" if field in {"prompt", "until"} else 99
        elif change == "task_id":
            r.task["id"] = ""
        elif change == "target":
            r.task["source"] = "agent_id=hermes:default;session_id=default/other"
        else:
            r.task[change] = "different"
    with pytest.raises(RuntimeError, match="handoff context was not read back"):
        await work.receipt(r.runner, r.source, r.state, r.item, r.dest)
    assert len(r.creates) == 1 and not r.sends


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reassigned", "report_to", "missing_history", "wrong_readback"])
async def test_task_transfer_stops_on_changed_or_unreportable_assignment(runtime, monkeypatch, change):
    r = runtime
    record = {"id": "t1", "assignment_id": "a1", "assigned_by": "hermes:default/parent", "report_to": "hermes:default/parent"}
    current = {"id": "a1", "assigned_to": "hermes:default/old", "assigned_by": record["assigned_by"],
               "report_to": record["report_to"], "accepted_at": "accepted"}
    if change == "reassigned":
        current["id"] = "different"
    if change == "report_to":
        record["report_to"] = "hermes:default/other"
    command = AsyncMock(side_effect=([[]] if change == "missing_history" else [[current], {}, [current]]))
    monkeypatch.setattr(work, "cli", command)
    with pytest.raises(RuntimeError, match="assignment"):
        await work.assign_task(r.runner, r.source, r.state, "t1", "hermes:default/next", record)
    if change != "wrong_readback":
        assert all("assign" not in call.args for call in command.await_args_list)


def test_handoff_key_is_bounded_stable_and_keeps_distinct_original_bindings():
    import re

    state = {"generation": "new", "handoff_generation": "original"}
    item = {"id": "task:" + "a/b:é" * 100}
    key = work.handoff_key(state, item)
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", key)
    assert key == work.handoff_key({**state, "generation": "retry"}, item)
    assert key != work.handoff_key(state, {"id": item["id"] + ":different"})


def test_live_process_transfer_updates_watcher_and_checkpoint(monkeypatch):
    from tools.process_registry import ProcessRegistry, ProcessSession
    registry = ProcessRegistry()
    session = ProcessSession(id="proc-retire", command="build", task_id="session:old-key",
                             owner_task_id="old", session_key="old-key", parent_session_id="old")
    registry._running[session.id] = session
    registry.pending_watchers.append({"session_id": session.id, "session_key": "old-key"})
    checkpoint = MagicMock()
    monkeypatch.setattr(registry, "_write_checkpoint", checkpoint)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="101", thread_id="18", chat_type="dm", user_id="101")
    moved = registry.transfer_ownership(session.id, from_owner="old", to_owner="next", to_task_id="session:next-key",
                                        to_session_key="next-key", to_source=source)
    assert moved is session and session.owner_task_id == "next"
    assert session.watcher_thread_id == "18" and session.parent_session_id == "next"
    assert registry.pending_watchers[0]["thread_id"] == "18"
    checkpoint.assert_called_once()


@pytest.mark.asyncio
async def test_process_watcher_delivers_only_to_transferred_topic(monkeypatch):
    from contextlib import nullcontext
    from tests.gateway.restart_test_helpers import make_restart_runner
    import tools.process_registry as registry_module
    from tools.process_registry import ProcessRegistry, ProcessSession

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc-route", command="build", task_id="task", owner_task_id="old",
        session_key="old-key", parent_session_id="old",
        watcher_platform="telegram", watcher_chat_id="101", watcher_thread_id="17")
    registry._running[session.id] = session
    monkeypatch.setattr(registry_module, "process_registry", registry)
    monkeypatch.setattr(registry, "_write_checkpoint", MagicMock())
    destination = SessionSource(platform=Platform.TELEGRAM, chat_id="101", thread_id="18", chat_type="dm")
    moved = registry.transfer_ownership(
        session.id, from_owner="old", to_owner="next", to_task_id="task",
        to_session_key="next-key", to_source=destination)
    assert moved is session
    session.mark_exited(0)
    runner, _adapter = make_restart_runner()
    monkeypatch.setattr(runner, "_completion_event_scope", lambda _: nullcontext())
    monkeypatch.setattr(runner, "_load_background_notifications_mode", lambda: "concise")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_send_watcher_message", send)
    monkeypatch.setattr("gateway.run_notifications.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("tools.process_registry_notifications.should_surface_notification", lambda _: True)
    await runner._run_process_watcher({
        "session_id": session.id, "session_key": "old-key", "check_interval": 1,
        "platform": "telegram", "chat_id": "101", "thread_id": "17"})
    send.assert_awaited_once()
    assert send.await_args is not None
    assert send.await_args.args[:3] == ("telegram", "101", "18")


@pytest.mark.asyncio
async def test_queued_batch_regroups_transferred_sibling_and_retries_only_new_route(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from tests.gateway.restart_test_helpers import make_restart_runner
    from tools import async_delegation as delegation

    old_key = "agent:main:telegram:dm:101:17"
    new_key = "agent:main:telegram:dm:101:18"
    monkeypatch.setattr(delegation, "_db_path", lambda: tmp_path / "state.db")
    with delegation._transaction() as conn:
        conn.executemany("""INSERT INTO async_delegations
            (delegation_id, origin_session, parent_session_id, state, dispatched_at, updated_at,
             delivery_state, task_json) VALUES (?, ?, ?, 'running', 1, 1, 'pending', '{}')""",
            [("stay", old_key, "old"), ("move", old_key, "old")])
    delegation.transfer_retirement_owner("move", old_key, "old", new_key, "next")
    group = [{"type": "async_delegation", "delegation_id": name, "session_key": old_key,
              "parent_session_id": "old", "platform": "telegram", "chat_id": "101", "thread_id": "17"}
             for name in ("stay", "move")]
    runner, _adapter = make_restart_runner()
    monkeypatch.setattr(runner, "_completion_event_scope", lambda _: nullcontext())
    deliver = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(runner, "_deliver_async_delegation_group_scoped", deliver)
    assert await runner._deliver_async_delegation_group(group) is False
    calls = deliver.await_args_list
    assert [call.args[0][0]["thread_id"] for call in calls] == ["17", "18"]
    assert [call.args[0][0]["parent_session_id"] for call in calls] == ["old", "next"]
    assert len(group) == 1 and group[0]["delegation_id"] == "move"
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_deliver_async_delegation_group_scoped", deliver)
    assert await runner._deliver_async_delegation_group(group) is True
    assert deliver.await_args is not None
    assert deliver.await_args.args[0][0]["thread_id"] == "18"


def test_durable_delegation_handoff_and_queued_snapshot_refresh(tmp_path, monkeypatch):
    from tools import async_delegation as delegation
    monkeypatch.setattr(delegation, "_db_path", lambda: tmp_path / "state.db")
    with delegation._transaction() as conn:
        conn.execute("""INSERT INTO async_delegations
            (delegation_id, origin_session, parent_session_id, state, dispatched_at, updated_at,
             delivery_state, task_json) VALUES (?, ?, ?, 'running', 1, 1, 'pending', ?)""",
            ("retirement-test", "old-key", "old", json.dumps({"goal": "Build Service"})))
    assert delegation.retirement_inventory("old-key", "old")[0]["goal"] == "Build Service"
    delegation.transfer_retirement_owner("retirement-test", "old-key", "old", "next-key", "next")
    delegation.transfer_retirement_owner("retirement-test", "old-key", "old", "next-key", "next")
    assert delegation.retirement_inventory("old-key", "old") == []
    event = {"type": "async_delegation", "delegation_id": "retirement-test", "session_key": "old-key",
             "parent_session_id": "old", "platform": "telegram", "thread_id": "17"}
    delegation.refresh_retirement_route(event)
    assert event["session_key"] == "next-key" and event["parent_session_id"] == "next"
    assert "thread_id" not in event
    with pytest.raises(RuntimeError, match="ownership changed"):
        delegation.transfer_retirement_owner("retirement-test", "wrong-key", "old", "elsewhere", "another")
