"""Discover topic-owned work before retirement and move supported durable owners.

Unknown inventories and transfers fail closed. A relay is not a transfer of a
scheduler callback or process owner, and must never be treated as one.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from hermes_platform.resolver import locate_command

from gateway.telegram_retire import save
from gateway.telegram_retire_context import authored_definition, context_identity, task_context


def address(source, session_id):
    return f"hermes:{source.profile or 'default'}/{session_id}"


# A Hermes relay is normally `posted` until the target session stores it, so
# reconcile for a bounded time instead of failing a retirement that is about to
# confirm. Each pass is bounded; housekeeping resumes receipts still in flight.
# The subprocess deadline must outlast relay's own wait.
RELAY_CONFIRM_WAIT = 60


class HandoffPending(RuntimeError):
    """The same relay receipt needs reconciliation on a later retirement pass."""


async def cli(runner, source, *args, timeout=30):
    from tools.environments.local import served_profile_child_env
    home = runner._resolve_profile_home_for_source(source)
    env = served_profile_child_env(target_home=home, inherit_credentials=True)
    session_id = await asyncio.to_thread(
        runner.session_store.peek_session_id, runner._session_key_for_source(source))
    env.update(HERMES_SESSION_ID=session_id or "", HERMES_SESSION_PROFILE=source.profile or "default",
               HERMES_SESSION_KEY=runner._session_key_for_source(source))
    executable = locate_command(args[0]).command
    if not executable:
        raise RuntimeError(f"{args[0]} is unavailable")
    proc = await asyncio.create_subprocess_exec(*executable, *args[1:], env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed: {stderr.decode(errors='replace')[:300]}")
    result = json.loads(stdout)
    if isinstance(result, dict):
        if result.get("error"):
            raise RuntimeError(f"{args[0]}: {result['error']}")
        return result.get("data", result)
    return result


class WorkInProgress(RuntimeError):
    """Execution tokens must settle normally, not be transferred or discarded."""


def open_control(raw):
    """Use the inventory's terminal semantics for successor admission too."""
    return raw is not None and json.loads(raw).get("status") not in {"cleared", "done", "completed", "stopped"}


async def inventory(runner, source, state) -> list[dict]:
    from cron.jobs import list_jobs, is_terminal_job
    from tools.async_delegation import retirement_inventory
    from tools.process_registry import process_registry

    sid, key = state["session_id"], state["session_key"]
    items = []
    for kind in ("goal", "loop"):
        raw = await runner._session_db.get_meta(f"{kind}:{sid}")
        if raw:
            record = json.loads(raw)
            if open_control(raw):
                items.append({"id": kind, "kind": kind, "title": record.get("goal") or record.get("prompt") or kind,
                              "record": record})
    origin_address = address(source, sid)
    for job in await asyncio.to_thread(list_jobs, include_disabled=True):
        if is_terminal_job(job):
            continue
        origin = job.get("origin") or {}
        direct = f"telegram:{source.chat_id}:{source.thread_id}"
        points_here = (str(origin.get("platform")) == "telegram"
                       and str(origin.get("chat_id")) == str(source.chat_id)
                       and str(origin.get("thread_id")) == str(source.thread_id))
        attached = str(job.get("attach_to_session") or "") == sid
        delivery = any(direct in (job.get(field) if isinstance(job.get(field), list) else [job.get(field)])
                       for field in ("deliver", "failure_deliver"))
        embedded = origin_address in json.dumps(authored_definition(job))
        if points_here or attached or delivery or embedded:
            items.append({"id": "cron:" + job["id"], "kind": "cron", "title": job.get("name") or job["id"],
                          "record": job})
    for process in await asyncio.to_thread(process_registry.list_sessions, session_key=key):
        if process["status"] == "running":
            items.append({"id": "process:" + process["session_id"], "kind": "process", "title": process["command"],
                          "record": process})
    for delegation in await asyncio.to_thread(retirement_inventory, key, sid):
        items.append({"id": "delegation:" + delegation["delegation_id"], "kind": "delegation",
                      "title": delegation.get("goal") or "Background delegation", "record": delegation})
    if locate_command("tasks").command:
        claims = await cli(runner, source, "tasks", "--json", "claims", "--session", origin_address)
        if not isinstance(claims, list):
            raise RuntimeError("Task claim inventory returned an incomplete response")
        if claims:
            raise WorkInProgress("Active task execution must settle before its topic retires. No claim token was reassigned.")
        tasks = await cli(runner, source, "tasks", "--json", "inbox", "--session", origin_address)
        if not isinstance(tasks, list):
            raise RuntimeError("Task inventory returned an incomplete response")
        for task in tasks:
            items.append({"id": "task:" + str(task["id"]), "kind": "task", "title": task["title"], "record": task})
    return items


async def destination(runner, source, selector):
    """Use the existing exact-title/live-binding lookup, not recency or word overlap."""
    candidates = await runner._session_db.list_live_telegram_topic_title_candidates(
        chat_id=str(source.chat_id), topic_name=selector, profile_name=source.profile or "default")
    candidates = [candidate for candidate in candidates if str(candidate["thread_id"]) != str(source.thread_id)]
    if len(candidates) != 1:
        raise ValueError(f"Owner '{selector}' is not 1 unambiguous live topic in this chat.")
    candidate = candidates[0]
    # The existing live matcher validates archive/profile/session state. Reject
    # another retirement too, so simultaneous retirements cannot hand off in a loop.
    from gateway.telegram_retire import load
    owner_source = replace(source, thread_id=str(candidate["thread_id"]))
    pending = await load(runner, owner_source)
    if pending and pending["phase"] not in {"cancelled", "done", "failed"}:
        raise ValueError(f"Owner '{selector}' is also retiring.")
    return candidate


def handoff_key(state, item):
    # Relay accepts a <=200-character plain token, not task/route addresses.
    binding = json.dumps([state.get("handoff_generation", state["generation"]), item["id"]])
    return "retire-" + hashlib.sha256(binding.encode("utf-8")).hexdigest()


def task_actor_args(actor):
    """Use the ledger's original native attribution, not the retiring session."""
    if actor.startswith("hermes:"):
        agent, separator, session = actor.partition("/")
    else:
        agent, separator, session = actor.partition(":")
    if not separator or not session:
        raise RuntimeError("Task assignment has no native reporting identity")
    return ("--agent-id", agent, "--session-id", session)


async def assign_task(runner, source, state, task_id, target, record=None):
    actor = record.get("assigned_by") if record else target
    if not actor or (record and actor != record.get("report_to")):
        raise RuntimeError(f"Task {task_id} has no reportable original assignment")
    # External requesters keep their reporting route. A self-assigned task's
    # topic-local result belongs in the replacement topic, not the deleted one.
    origin = address(source, state["session_id"])
    if actor == origin:
        actor = target
    expected = {"assigned_to": target, "assigned_by": actor, "report_to": actor}

    async def latest():
        rows = await cli(runner, source, "tasks", "--json", "assignments", task_id)
        if not isinstance(rows, list) or (record and not rows):
            raise RuntimeError(f"Task {task_id} has no readable assignment history")
        return rows[-1] if rows else {}

    current = await latest()
    if all(current.get(key) == value for key, value in expected.items()):
        return
    if current and (not record or not record.get("assignment_id")
            or current.get("id") != record["assignment_id"]
            or current.get("assigned_to") != origin
            or current.get("assigned_by") != record["assigned_by"]
            or current.get("report_to") != record["report_to"]):
        raise RuntimeError(f"Task {task_id} assignment changed before transfer")
    # The ledger allows only 1 pending offer. Acceptance is not execution and
    # releases that offer without claiming or settling the original task.
    if current and not current.get("accepted_at"):
        await cli(runner, source, "tasks", "--json", "accept", task_id)
    await cli(runner, source, "tasks", "--json", *task_actor_args(actor), "assign", task_id,
              "--to", target, "--report-to", actor)
    assigned = await latest()
    if not all(assigned.get(key) == value for key, value in expected.items()):
        raise RuntimeError(f"Task {task_id} reporting assignment was not read back")


def same_handoff_context(saved, expected, kind):
    """Compare the same authored identity for legacy briefs and compact manifests."""
    if saved == expected:
        return True
    if kind not in {"cron", "loop", "goal"}:
        return False
    try:
        prefix, wanted = expected.split("\n", 1)
        saved_prefix, actual = saved.split("\n", 1)
        actual, wanted = json.loads(actual), json.loads(wanted)
        if actual.get("definition_ref") and wanted.get("definition_ref"):
            return saved_prefix == prefix and actual == wanted
        return saved_prefix == prefix and context_identity(actual) == context_identity(wanted)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def original_handoff_context(context):
    """Separate the original structured brief from destination-appended notes."""
    try:
        prefix, payload = context.split("\n", 1)
        _, end = json.JSONDecoder().raw_decode(payload)
        return prefix + "\n" + payload[:end]
    except (AttributeError, TypeError, ValueError):
        return None


async def accepted_handoff_task(runner, source, saved, context, kind, target):
    """Read ownership and preserved content without ever editing destination work."""
    task_id = saved["id"]
    rows = await cli(runner, source, "tasks", "--json", "assignments", task_id)
    if not isinstance(rows, list):
        raise RuntimeError(f"Task {task_id} has no readable assignment history")
    assignment = rows[-1] if rows else {}
    if assignment and any(assignment.get(key) != target for key in ("assigned_to", "assigned_by", "report_to")):
        raise RuntimeError(f"Task {task_id} handoff assignment changed")
    original = original_handoff_context(saved.get("context"))
    current_preserved = same_handoff_context(original, context, kind)
    preserved = current_preserved
    appended = preserved and original != saved.get("context")
    accepted = bool(assignment.get("accepted_at")) or saved.get("status") in {
        "in_progress", "waiting", "blocked", "needs_review", "done", "cancelled"}
    if not preserved or (assignment and not (accepted or appended)):
        history = await cli(runner, source, "tasks", "--json", "log", task_id)
        events = history.get("events") if isinstance(history, dict) else None
        if not isinstance(events, list):
            raise RuntimeError(f"Task {task_id} has no readable history")
        for event in events:
            if event.get("task_id") != task_id:
                continue
            data = event.get("event_data") or {}
            preserved |= same_handoff_context(original_handoff_context(data.get("context")), context, kind)
            accepted |= event.get("actor") == target and event.get("event_type") == "updated"
    if not preserved or (not current_preserved and not accepted):
        raise RuntimeError(f"Task {task_id} handoff context was not read back")
    # Acceptance must belong to this exact destination assignment. A matching
    # creator alone is not proof that the destination owns the delivered task.
    if (accepted or appended) and not assignment:
        raise RuntimeError(f"Task {task_id} has no destination assignment")
    return bool(accepted or appended)


async def receipt(runner, source, state, item, dest):
    """Preserve work as tasks, or context alone as a non-actionable merge settlement."""
    handoff = state.get("handoffs", {}).get(item["id"], {})
    if handoff.get("receipt_id") and item["kind"] not in {"goal", "loop"}:
        return await confirm_receipt(runner, source, handoff, dest)
    target = address(source, dest["session_id"])
    if item["kind"] == "context":
        body = ("📨 Topic merged\nThe retired topic's lasting subject now lives here. "
                "This context transfer requests no new work or reply.\n---\n"
                + json.dumps(item["record"], ensure_ascii=False))
        return await send_receipt(runner, source, state, item, dest, body, ("--fyi",))
    if item["kind"] == "task":
        task_id = str(item["record"]["id"])
    else:
        # tasks handoff reports to its sender, and relay refuses a self-send.
        # Create/assign under the destination, then relay from the source.
        owner_source = replace(source, thread_id=str(dest["thread_id"]))
        owner_key = runner._session_key_for_source(owner_source)
        if (owner_key != dest["session_key"]
                or await asyncio.to_thread(runner.session_store.peek_session_id, owner_key) != dest["session_id"]):
            raise RuntimeError("Handoff destination binding changed")
        title = f"Retired topic: {item['kind']}"
        context = task_context(source, state, item)
        key = handoff_key(state, item)
        # Resolve an existing key before add, so only a proven dedupe hit may
        # have older loop progress. A fresh create still requires full content.
        # The query language reads "word:" as a field filter even inside quotes, so search the
        # colon-free words and match the key here. ``--all`` returns every page.
        matches = await cli(runner, owner_source, "tasks", "--json", "query",
                            f'"Retired topic" {item["kind"]}', "--all")
        if isinstance(matches, dict) and not matches.get("has_more") and not matches.get("truncated"):
            matches = matches.get("items")
        if not isinstance(matches, list):
            raise RuntimeError("Handoff task lookup returned an incomplete response")
        matches = [task for task in matches if task.get("dedupe_key") == key]
        if len(matches) > 1:
            raise RuntimeError("Handoff task key is ambiguous")
        prior = matches[0] if matches else None
        if prior is None and (handoff.get("receipt_id") or handoff.get("accepted_task_id")):
            raise RuntimeError("Delivered handoff task was not found by its key")
        created = prior if prior is not None else await cli(
            runner, owner_source, "tasks", "--json", "add", title,
            "--context", context, "--dedupe-key", key)
        task_id = str(created.get("id") or "")
        if not task_id or (handoff.get("accepted_task_id") and handoff["accepted_task_id"] != task_id):
            raise RuntimeError(f"Task {task_id} handoff context was not read back")
        saved = await cli(runner, source, "tasks", "--json", "show", task_id)
        profile = source.profile or "default"
        creator = f"agent_id=hermes:{profile};session_id={profile}/{dest['session_id']}"
        if (not task_id or saved.get("id") != task_id or saved.get("title") != title
                or saved.get("dedupe_key") != key or saved.get("source") != creator):
            raise RuntimeError(f"Task {task_id} handoff context was not read back")
        if prior is None and saved.get("context") != context and original_handoff_context(saved.get("context")) != context:
            raise RuntimeError(f"Task {task_id} handoff context was not read back")
        if prior is not None or saved.get("context") != context or saved.get("status", "todo") != "todo":
            if await accepted_handoff_task(runner, source, saved, context, item["kind"], target):
                return {"accepted_task_id": task_id, "address": target, "destination": dest}
    if handoff.get("receipt_id"):
        return await confirm_receipt(runner, source, handoff, dest)
    await assign_task(runner, source, state, task_id, target,
                      item["record"] if item["kind"] == "task" else None)
    # The full resumption context is already in the canonical ledger.
    body = f"📦 Topic handoff\nContinue task {task_id} from the retiring topic. Read its current ledger context before acting."
    return await send_receipt(runner, source, state, item, dest, body, ("--task", task_id))


async def send_receipt(runner, source, state, item, dest, body, intent):
    """Use the same idempotency and exact-transcript receipt gate for either intent."""
    target = address(source, dest["session_id"])
    from pathlib import Path
    from tempfile import NamedTemporaryFile
    from hermes_constants import get_hermes_home
    scratch = get_hermes_home() / "cache" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=scratch, suffix=".txt", delete=False) as payload:
        payload.write(body)
        path = Path(payload.name)
    try:
        sent = await cli(runner, source, "relay", "--json", "send", target, *intent,
                         "--key", handoff_key(state, item), "--body-file", str(path))
    finally:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    receipt_id = sent.get("id")
    if not receipt_id:
        raise RuntimeError("Handoff did not return a relay receipt")
    # Persist before the bounded wait, including when polling fails or the turn
    # is interrupted. Recovery must read this receipt, not send the handoff again.
    handoff = state.setdefault("handoffs", {}).setdefault(item["id"], {"item": item, "moved": False})
    handoff.update(receipt_id=receipt_id, address=target, destination=dest)
    await save(runner, source, state)
    return await confirm_receipt(runner, source, handoff, dest)


async def confirm_receipt(runner, source, handoff, dest):
    target = address(source, dest["session_id"])
    receipt_id = handoff["receipt_id"]
    if handoff.get("address") != target:
        raise RuntimeError("Handoff receipt destination changed")
    readback = await cli(runner, source, "relay", "--json", "status", "--wait", f"{RELAY_CONFIRM_WAIT}s",
                         str(receipt_id), timeout=RELAY_CONFIRM_WAIT + 30)
    if readback.get("id") != receipt_id or readback.get("target") != target:
        raise RuntimeError(f"Handoff receipt {receipt_id} is not confirmed at {target}: receipt identity mismatch")
    status = readback.get("status")
    if status in {"pending", "posted", "sent_unconfirmed"}:
        raise HandoffPending(f"Handoff receipt {receipt_id} is not confirmed at {target}: {status}")
    if status not in {"delivered", "replied"} or readback.get("error"):
        raise RuntimeError(f"Handoff receipt {receipt_id} is not confirmed at {target}: {status}")
    return {"receipt_id": receipt_id, "address": target, "destination": dest}


async def transfer_control(runner, source, state, item, dest):
    """Settle source control and replay proof in one store transaction.

    Continuation migration is best-effort and rewrites writer-owned tokens. An
    unaccepted retirement handoff needs an atomic, snapshot-checked move. An
    accepted destination task instead owns its successor control independently.
    """
    from gateway.telegram_retire import RetirementSuperseded, state_key
    from hermes_cli.goals import GoalState, save_goal
    from hermes_cli.loops import LoopState, save_loop

    kind = item["kind"]
    old_key, new_key = f"{kind}:{state['session_id']}", f"{kind}:{dest['session_id']}"
    db = runner._session_db._db

    def read(conn, key):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def move(conn):
        raw = read(conn, state_key(source))
        stored = json.loads(raw) if raw else json.loads(json.dumps(state))
        if stored["generation"] != state["generation"]:
            raise RetirementSuperseded
        handoff = stored["handoffs"].setdefault(item["id"], {"item": item, "destination": dest, "moved": False})
        old, new = read(conn, old_key), read(conn, new_key)
        proof = handoff.get("control_receipt")
        accepted_task_id = handoff.get("accepted_task_id")
        if proof:
            expected = {"source": old, "session_id": dest["session_id"]}
            if proof.get("ended"):
                expected["ended"] = True
            elif accepted_task_id:
                expected["accepted_task_id"] = accepted_task_id
            else:
                expected["destination"] = new
            if proof == expected:
                return proof
            raise RuntimeError(f"Transferred {kind} changed before retirement recovery")
        if not old:
            raise RuntimeError(f"Cannot transfer {kind}: the source control disappeared")
        if not open_control(old):
            # It finished or was cleared after the handoff was recorded. The handoff task already
            # carries its definition; there is no live control left to move or clear.
            proof = {"source": old, "session_id": dest["session_id"], "ended": True}
            handoff["control_receipt"] = proof
            db.set_meta(state_key(source), json.dumps(stored), cursor=conn)
            return proof
        # A running loop or goal advances its counters every tick. Only its authored definition
        # and status say whether it is still the work that was handed off (2026-10-30: two
        # retirements failed forever because a loop ticked after its handoff was recorded).
        current = json.loads(old)
        if (authored_definition(current) != authored_definition(item["record"])
                or current.get("status") != item["record"].get("status")):
            raise RuntimeError(f"Cannot transfer {kind}: the source changed after its handoff was recorded")
        if not accepted_task_id and open_control(new):
            raise RuntimeError(f"Cannot transfer {kind}: destination already owns one")
        if kind == "loop":
            current_loop, retired_loop = LoopState.from_json(old), LoopState.from_json(old)
            retired_loop.status = "cleared"
            retired_loop.awaiting_response = False
            if not accepted_task_id:
                current_loop.route = {**current_loop.route, "platform": source.platform.value,
                                      "chat_id": str(source.chat_id), "chat_type": source.chat_type,
                                      "thread_id": str(dest["thread_id"])}
                save_loop(dest["session_id"], current_loop, cursor=conn)
            save_loop(state["session_id"], retired_loop, cursor=conn)
        else:
            current_goal, retired_goal = GoalState.from_json(old), GoalState.from_json(old)
            retired_goal.status = "cleared"
            if not accepted_task_id:
                save_goal(dest["session_id"], current_goal, cursor=conn)
            save_goal(state["session_id"], retired_goal, cursor=conn)
        # An accepted task owns its successor control. Clear only the source,
        # and never pin recovery to the destination's independently ticking loop.
        proof = {"source": read(conn, old_key), "session_id": dest["session_id"]}
        if accepted_task_id:
            proof["accepted_task_id"] = accepted_task_id
        else:
            proof["destination"] = read(conn, new_key)
        handoff["control_receipt"] = proof
        db.set_meta(state_key(source), json.dumps(stored), cursor=conn)
        return proof

    proof = await runner._session_db._execute_write(move)
    state["handoffs"].setdefault(item["id"], {"item": item, "destination": dest, "moved": False})["control_receipt"] = proof


async def transfer(runner, source, state, item, dest):
    """Change the actual owner. Refuse conflicts rather than overwrite obligations."""
    from cron.jobs import get_job, update_job
    from tools.process_registry import process_registry
    old_sid, new_sid = state["session_id"], dest["session_id"]
    kind = item["kind"]
    if kind in {"goal", "loop"}:
        await transfer_control(runner, source, state, item, dest)
    elif kind == "cron":
        job_id = item["record"]["id"]
        current = await asyncio.to_thread(get_job, job_id)
        if current is None:
            raise RuntimeError(f"Cron {job_id} disappeared before transfer")
        before = json.dumps(current)
        old_address, new_address = address(source, old_sid), address(source, new_sid)
        old_delivery = f"telegram:{source.chat_id}:{source.thread_id}"
        new_delivery = f"telegram:{source.chat_id}:{dest['thread_id']}"
        updates = {}
        origin = current.get("origin") or {}
        if str(origin.get("chat_id")) == str(source.chat_id) and str(origin.get("thread_id")) == str(source.thread_id):
            updates["origin"] = {**origin, "thread_id": str(dest["thread_id"])}
        for field in ("deliver", "failure_deliver", "prompt"):
            if isinstance(current.get(field), str):
                if field == "prompt":
                    updates[field] = current[field].replace(old_address, new_address)
                else:
                    updates[field] = new_delivery if current[field] == old_delivery else current[field]
            elif isinstance(current.get(field), list):
                updates[field] = [new_delivery if value == old_delivery else value for value in current[field]]
        if str(current.get("attach_to_session") or "") == old_sid:
            updates["attach_to_session"] = new_sid
        await asyncio.to_thread(update_job, job_id, updates)
        readback = await asyncio.to_thread(get_job, job_id)
        if readback is None or any(readback.get(field) != value for field, value in updates.items()):
            raise RuntimeError(f"Cron {job_id} transfer did not read back")
        if before == json.dumps(readback) and (old_address in before or old_delivery in before):
            raise RuntimeError(f"Cron {job_id} has no supported movable destination")
    elif kind == "delegation":
        from tools.async_delegation import transfer_retirement_owner
        await asyncio.to_thread(transfer_retirement_owner, item["record"]["delegation_id"],
                               state["session_key"], old_sid, dest["session_key"], new_sid)
    elif kind == "process":
        record = item["record"]
        # Destination binding identifies its stable gateway key.
        entries = await asyncio.to_thread(runner.session_store.list_sessions)
        target_entry = next((entry for entry in entries if entry.session_id == new_sid), None)
        if target_entry is None:
            raise RuntimeError("Process destination has no live routing entry")
        current = await asyncio.to_thread(process_registry.get, record["session_id"])
        if current and current.session_key == target_entry.session_key and current.owner_task_id == new_sid:
            return
        moved = await asyncio.to_thread(process_registry.transfer_ownership, record["session_id"],
            from_owner=record["owner_task_id"], to_owner=new_sid, to_task_id="session:" + target_entry.session_key,
            to_session_key=target_entry.session_key, note="Topic retirement",
            to_source=target_entry.origin)
        if moved is None or moved.session_key != target_entry.session_key:
            raise RuntimeError("Process ownership changed before transfer")


async def recover_context_receipt(runner, source, state):
    """Older failures saved the receipt in error text before saving the map."""
    match = re.fullmatch(
        r"Handoff receipt ([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}) "
        r"is not confirmed at (\S+?)(?:: .*)?", state.get("error") or "")
    if not match:
        return
    receipt_id, target = match.groups()
    handoffs = state["handoffs"].values()
    if any(handoff.get("receipt_id") == receipt_id for handoff in handoffs):
        return
    candidates = [handoff for handoff in handoffs
                  if handoff.get("item", {}).get("kind") == "context"
                  and not handoff.get("receipt_id") and not handoff.get("moved")
                  and address(source, handoff["destination"]["session_id"]) == target]
    if len(candidates) != 1:
        raise RuntimeError(f"Handoff receipt {receipt_id} has no unambiguous saved context destination")
    # This records an existing receipt identity, not a delivery claim. The normal
    # exact-target status read below must still confirm delivered/replied.
    candidates[0].update(receipt_id=receipt_id, address=target)
    await save(runner, source, state)


async def settle_handoffs(runner, source, state):
    await recover_context_receipt(runner, source, state)
    # Reconcile receipts independently of source inventory. A crash after moving
    # an item must not forget a still-unconfirmed handoff just because it moved.
    for item_id, handoff in state["handoffs"].items():
        if (handoff.get("receipt_id") or handoff.get("accepted_task_id")) and handoff.get("moved"):
            continue
        item, dest = handoff["item"], handoff["destination"]
        sent = await receipt(runner, source, state, item, dest)
        handoff.update(sent)
        await save(runner, source, state)
        if not handoff.get("moved"):
            await transfer(runner, source, state, item, dest)
            handoff["moved"] = True
            await save(runner, source, state)


async def prepare(runner, source, state):
    from gateway.telegram_retire_prepare import queue_preparation
    await settle_handoffs(runner, source, state)
    try:
        items = await inventory(runner, source, state)
    except WorkInProgress as exc:
        return await queue_preparation(runner, source, state, [], busy=str(exc))
    if not state.get("reason") or (state.get("target") and not state.get("context")):
        return await queue_preparation(runner, source, state, items)
    owners = state.setdefault("owners", {})
    unresolved, selected = [], []
    if state.get("target"):
        try:
            dest = await destination(runner, source, state["target"])
        except ValueError as exc:
            return await queue_preparation(runner, source, state, items,
                busy=f"Merge destination {state['target']} is unavailable: {exc}")
        state["merge_destination"] = dest
        context = {"id": "context:" + dest["session_id"], "kind": "context",
                   "title": "Topic context", "record": {
                       "source_session": state["session_id"], "source_thread": str(source.thread_id),
                       "reason": state["reason"],
                       "summary": state["context"],
                       "history": "Prior decisions, evidence and limits remain searchable in source_session."}}
        if context["id"] not in state["handoffs"]:
            selected.append((context, dest))
    for item in items:
        if item["id"] in state["handoffs"]:
            raise RuntimeError(f"Previously handed-off work still targets this topic: {item['id']}")
        selector = owners.get(item["id"]) or state.get("target")
        if not selector:
            # An exact-title hit is a candidate, not proof of subject ownership.
            # The normal session turn verifies the context and records a choice.
            unresolved.append(item)
            continue
        try:
            dest = await destination(runner, source, selector)
        except ValueError as exc:
            unresolved.append({**item, "blocked_owner": selector, "blocked_reason": str(exc)})
            continue
        if item["kind"] in {"goal", "loop"} and open_control(await runner._session_db.get_meta(
                f"{item['kind']}:{dest['session_id']}")):
            unresolved.append({**item, "blocked_owner": dest["title"],
                               "blocked_reason": "Destination already has a control. Choose a compatible successor, do not overwrite it."})
            continue
        selected.append((item, dest))
    for item, dest in selected:
        # Save the intent before either external side effect. Both receipt and
        # assignment routes have stable keys and readbacks on restart.
        handoff = {"item": item, "destination": dest, "moved": False}
        state["handoffs"][item["id"]] = handoff
        await save(runner, source, state)
        handoff.update(await receipt(runner, source, state, item, dest))
        await save(runner, source, state)
        await transfer(runner, source, state, item, dest)
        handoff["moved"] = True
        await save(runner, source, state)
    if unresolved:
        return await queue_preparation(runner, source, state, unresolved)
    # Repeating discovery catches work created by a final tool call and stale
    # destinations. Nothing can be deleted on an incomplete/failed inventory.
    if await inventory(runner, source, state):
        raise RuntimeError("Open work still points at this topic after handoff")
    return True
