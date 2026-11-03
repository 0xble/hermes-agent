"""The task ledger receives authored definitions, not cron execution output."""
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from gateway import telegram_retire as retire
from gateway import telegram_retire_work as work
from tests.gateway.test_telegram_retire_work import runtime


# public.task_events.ck_task_events_data, migration
# 20260730190000_redesign_tasks_project_free.sql:157. The tasks pgfake does
# not implement this constraint (its created event stores only source).
EVENT_DATA_LIMIT = 16384


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,prompt_size", [("cron", 200), ("cron", 30000), ("loop", 30000)])
async def test_large_job_handoff_respects_ledger_contract(runtime, monkeypatch, kind, prompt_size):
    r = runtime
    prompt = "Preserve the full obligation. " * (prompt_size // 29 + 1)
    record = {"id": "3a127466598c", "name": "Daily Journal", "prompt": prompt,
              "schedule": {"kind": "cron", "expr": "0 8 * * *"},
              "repeat": {"times": 12, "completed": 8}, "until": "Journal is complete",
              "mode": "interval", "interval_seconds": 3600, "times": 12, "max_ticks": 20,
              "skills": ["writing"], "deliver": "telegram:101:17", "model": "test-model",
              "latest_execution": {"output": "Collected journal output " * 9000},
              "run_claim": {"token": "volatile"}, "ticks_fired": 8, "created_at": 123,
              "next_due_at": 456, "last_run_at": 789}
    assert len(json.dumps(record).encode()) > 219000
    item = {"id": "cron:" + record["id"] if kind == "cron" else "loop", "kind": kind,
            "title": record["name"], "record": record}
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18"}
    r.state["handoffs"][item["id"]] = {"item": item, "destination": dest, "moved": False}
    await retire.save(r.runner, r.source, r.state)
    task = {}
    adds = []

    async def command(_runner, _source, *args, **_kwargs):
        if args[2] == "query":
            return [dict(task)] if task else []
        if args[2] == "add":
            context = args[args.index("--context") + 1]
            payload = {"title": args[3], "context": context, "tags": [], "assignees": [],
                       "source": "agent_id=hermes:default;session_id=default/next"}
            # tasks/ops/tasks.go:addPayload and task_create record this payload
            # as created.event_data. The dedupe key is a separate RPC argument.
            # Reserve space for jsonb headers as well as every actual add field.
            assert len(json.dumps(payload, ensure_ascii=False).encode()) + 512 <= EVENT_DATA_LIMIT
            adds.append(payload)
            task.update(payload, id="created-task", status="todo",
                        dedupe_key=args[args.index("--dedupe-key") + 1])
            return dict(task)
        if args[2] == "show":
            return dict(task)
        if args[2] == "log":
            return {"events": [{"task_id": "created-task", "event_type": "created",
                                "event_data": {"context": adds[0]["context"]}}]}
        if args[2] == "assignments":
            return [{"assigned_to": "hermes:default/next", "assigned_by": "hermes:default/next",
                     "report_to": "hermes:default/next", "accepted_at": "accepted"}]
        raise AssertionError(args)

    monkeypatch.setattr(work, "cli", command)
    monkeypatch.setattr(work, "assign_task", AsyncMock())
    monkeypatch.setattr(work, "send_receipt", AsyncMock(return_value={"receipt_id": "r1"}))
    await work.receipt(r.runner, r.source, r.state, item, dest)
    payload = json.loads(task["context"].split("\n", 1)[1])
    assert "latest_execution" not in task["context"] and "run_claim" not in task["context"]
    assert "ticks_fired" not in task["context"] and "created_at" not in task["context"]
    if prompt_size > EVENT_DATA_LIMIT:
        reference = payload["definition_ref"]
        saved = await retire.load(r.runner, r.source)
        full = saved["handoffs"][item["id"]]["item"]
        assert full["record"]["prompt"] == prompt
        assert reference["state_key"] == retire.state_key(r.source)
        assert reference["item_id"] == item["id"]
        from gateway.telegram_retire_context import stable_item
        canonical = json.dumps(stable_item(full), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert reference["sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    else:
        assert payload["record"]["prompt"] == prompt
        assert payload["record"]["repeat"] == {"times": 12}
        assert payload["record"]["schedule"] == record["schedule"]
        assert payload["record"]["skills"] == record["skills"]
        assert payload["record"]["model"] == record["model"]
    # A new execution snapshot must read back the same stable representation,
    # including accepted destination append-only amendments, without any edits.
    original = task["context"]
    task["context"] += "\nAccepted by the successor."
    record["latest_execution"]["output"] = "A different runtime output"
    record["ticks_fired"] += 1
    record["repeat"]["completed"] += 1
    await work.receipt(r.runner, r.source, r.state, item, dest)
    assert len(adds) == 1 and task["context"] == original + "\nAccepted by the successor."
    record["prompt"] += "A changed obligation"
    with pytest.raises(RuntimeError, match="handoff context was not read back"):
        await work.receipt(r.runner, r.source, r.state, item, dest)


@pytest.mark.asyncio
async def test_cron_inventory_ignores_execution_only_source_mentions(runtime, monkeypatch):
    from cron import jobs
    monkeypatch.setattr(jobs, "list_jobs", lambda **_: [{
        "id": "unrelated", "prompt": "Unrelated work", "deliver": "telegram:101:99",
        "latest_execution": {"output": "Old mention of hermes:default/old"}}])
    assert await work.inventory(runtime.runner, runtime.source, runtime.state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["posted", "delivered", "wrong_target", "wrong_id", "failed"])
async def test_error_only_context_receipt_is_read_not_resent(runtime, monkeypatch, status):
    r = runtime
    receipt_id = "8a03d0f8-09bd-480c-8be2-6fcaeb62df0c"
    item = {"id": "context:next", "kind": "context", "record": {"summary": "Original constraints"}}
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18"}
    r.state.update(error=f"Handoff receipt {receipt_id} is not confirmed at hermes:default/next: posted",
                   handoffs={item["id"]: {"item": item, "destination": dest, "moved": False}})
    response = {"id": receipt_id, "target": "hermes:default/next", "status": status}
    if status == "wrong_target":
        response.update(target="hermes:default/other", status="delivered")
    elif status == "wrong_id":
        response.update(id="other-receipt", status="delivered")
    command = AsyncMock(return_value=response)
    monkeypatch.setattr(work, "cli", command)
    if status == "delivered":
        await work.settle_handoffs(r.runner, r.source, r.state)
    else:
        with pytest.raises(RuntimeError, match="not confirmed"):
            await work.settle_handoffs(r.runner, r.source, r.state)
    assert command.await_args_list
    assert all(call.args[2:5] == ("relay", "--json", "status") for call in command.await_args_list)
    assert all(call.args[-1] == receipt_id for call in command.await_args_list)
    saved = await retire.load(r.runner, r.source)
    assert saved["handoffs"][item["id"]]["receipt_id"] == receipt_id
    assert bool(saved["handoffs"][item["id"]].get("moved")) == (status == "delivered")


@pytest.mark.asyncio
async def test_retry_keeps_error_only_receipt_before_replacing_generation(runtime, monkeypatch):
    r = runtime
    receipt_id = "8a03d0f8-09bd-480c-8be2-6fcaeb62df0c"
    dest = {"session_id": "next", "session_key": "next-key", "thread_id": "18"}
    item = {"id": "context:next", "kind": "context", "record": {"summary": "Original limits"}}
    r.state.update(phase="failed", error=f"Handoff receipt {receipt_id} is not confirmed at hermes:default/next",
                   handoffs={item["id"]: {"item": item, "destination": dest, "moved": False}})
    await retire.save(r.runner, r.source, r.state)
    command = AsyncMock(return_value={"id": receipt_id, "target": "hermes:default/next", "status": "delivered"})
    monkeypatch.setattr(work, "cli", command)
    await retire.request(r.runner, r.source, target="Successor", reason="Keep all prior limits.", context="Subject")
    retried = await retire.load(r.runner, r.source)
    assert retried["generation"] != r.state["generation"]
    assert retried["handoffs"][item["id"]]["receipt_id"] == receipt_id
    await work.settle_handoffs(r.runner, r.source, retried)
    command.assert_awaited_once()
    assert command.await_args.args[2:5] == ("relay", "--json", "status")


@pytest.mark.asyncio
async def test_ambiguous_error_receipt_refuses_any_send(runtime, monkeypatch):
    r = runtime
    receipt_id = "8a03d0f8-09bd-480c-8be2-6fcaeb62df0c"
    r.state["error"] = f"Handoff receipt {receipt_id} is not confirmed at hermes:default/next: posted"
    command = AsyncMock()
    monkeypatch.setattr(work, "cli", command)
    with pytest.raises(RuntimeError, match="no unambiguous saved context destination"):
        await work.settle_handoffs(r.runner, r.source, r.state)
    command.assert_not_awaited()


@pytest.mark.parametrize("kind", ["cron", "loop", "goal"])
@pytest.mark.parametrize("prompt", ["A normal definition", "\\\"\\n" * 6000, "متن" * 6000])
def test_legacy_and_new_context_share_authored_identity(runtime, kind, prompt):
    from gateway.telegram_retire_context import task_context
    r = runtime
    item = {"id": "item", "kind": kind, "title": "Authored subject", "record": {
        "id": "job-id", "prompt": prompt, "goal": prompt,
        "schedule": {"kind": "interval", "seconds": 3600}, "times": 9, "until": "Done",
        "contract": {"constraints": "Never spend", "verification": "Exact evidence"},
        "revisions": [{"at": 123, "reason": "User requested", "user_quote": "Keep this exact limit"}],
        "latest_execution": {"output": "volatile"}, "turns_used": 4}}
    old = "Own the transferred work. Preserve its original limits and report to this topic.\n" + json.dumps(item)
    current = task_context(r.source, r.state, item)
    assert work.same_handoff_context(old, current, kind)
    assert work.same_handoff_context(current, old, kind)
    item["record"]["contract"]["constraints"] = "Changed limit"
    assert not work.same_handoff_context(old, task_context(r.source, r.state, item), kind)
