"""Discover topic-owned work before retirement and move supported durable owners.

Unknown inventories and transfers fail closed. A relay is not a transfer of a
scheduler callback or process owner, and must never be treated as one.
"""
from __future__ import annotations

import asyncio
import json
import re
from hermes_platform.resolver import locate_command

from gateway.telegram_retire import save


def address(source, session_id):
    return f"hermes:{source.profile or 'default'}/{session_id}"


async def cli(runner, source, *args):
    from tools.environments.local import served_profile_child_env
    home = runner._resolve_profile_home_for_source(source)
    env = served_profile_child_env(target_home=home, inherit_credentials=True)
    session_id = runner.session_store.peek_session_id(runner._session_key_for_source(source))
    env.update(HERMES_SESSION_ID=session_id or "", HERMES_SESSION_PROFILE=source.profile or "default",
               HERMES_SESSION_KEY=runner._session_key_for_source(source))
    executable = locate_command(args[0]).command
    if not executable:
        raise RuntimeError(f"{args[0]} is unavailable")
    proc = await asyncio.create_subprocess_exec(*executable, *args[1:], env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
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


async def inventory(runner, source, state):
    from cron.jobs import list_jobs, is_terminal_job
    from tools.async_delegation import retirement_inventory
    from tools.process_registry import process_registry

    sid, key = state["session_id"], state["session_key"]
    items = []
    for kind in ("goal", "loop"):
        raw = await runner._session_db.get_meta(f"{kind}:{sid}")
        if raw:
            record = json.loads(raw)
            if record.get("status") not in {"cleared", "done", "completed", "stopped"}:
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
        embedded = origin_address in json.dumps(job)
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
            raise RuntimeError("Active task execution must settle before its topic retires. No claim token was reassigned.")
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
    from dataclasses import replace
    from gateway.telegram_retire import load
    owner_source = replace(source, thread_id=str(candidate["thread_id"]))
    pending = await load(runner, owner_source)
    if pending and pending["phase"] not in {"cancelled", "done", "failed"}:
        raise ValueError(f"Owner '{selector}' is also retiring.")
    return candidate


async def resolve_answers(runner, source, state, text):
    """Accept only a complete numbered owner answer, never start a new model turn."""
    matches = re.findall(r"(?:^|[;\n])\s*(\d+)[.):]?\s+([^;\n]+)", text.strip())
    if not matches:
        return False
    questions = state["questions"]
    selections = {}
    try:
        for number, name in matches:
            index = int(number) - 1
            if index < 0 or index >= len(questions) or index in selections:
                return False
            await destination(runner, source, name.strip())
            selections[index] = name.strip()
    except ValueError:
        return False
    if len(selections) != len(questions):
        return False
    state.setdefault("owners", {}).update({questions[index]["id"]: name for index, name in selections.items()})
    state.pop("questions", None)
    state["phase"] = "pending"
    await save(runner, source, state)
    return True


async def receipt(runner, source, state, item, dest):
    """Task-linked relay with stable keys, then exact receipt reconciliation."""
    target = address(source, dest["session_id"])
    if item["kind"] == "task":
        task_id = str(item["record"]["id"])
        await cli(runner, source, "tasks", "--json", "assign", task_id, "--to", target, "--report-to", target)
        assigned = await cli(runner, source, "tasks", "--json", "inbox", "--session", target)
        if not any(str(task.get("id")) == task_id for task in assigned):
            raise RuntimeError(f"Task {task_id} assignment was not read back")
        # Existing task's resumption context is already in the canonical ledger.
        body = f"📦 Topic handoff\nContinue task {task_id} from the retiring topic. Read its current ledger context before acting."
        from pathlib import Path
        from tempfile import NamedTemporaryFile
        from hermes_constants import get_hermes_home
        scratch = get_hermes_home() / "cache" / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=scratch, suffix=".txt", delete=False) as payload:
            payload.write(body)
            path = Path(payload.name)
        try:
            sent = await cli(runner, source, "relay", "--json", "send", target, "--task", task_id,
                             "--key", f"retire:{state.get('handoff_generation', state['generation'])}:{item['id']}", "--body-file", str(path))
        finally:
            path.unlink(missing_ok=True)
    else:
        sent = await cli(runner, source, "tasks", "--json", "handoff", target,
                         "--title", f"Retired topic: {item['kind']}",
                         "--brief", "Own the transferred work from the retiring topic. Preserve its original limits and report to this topic.",
                         "--context", json.dumps(item, ensure_ascii=False),
                         "--dedupe-key", f"retire:{state.get('handoff_generation', state['generation'])}:{item['id']}")
    relay = sent.get("relay", sent)
    receipt_id = relay.get("id")
    if not receipt_id:
        raise RuntimeError("Handoff did not return a relay receipt")
    readback = await cli(runner, source, "relay", "--json", "status", str(receipt_id))
    if (readback.get("id") != receipt_id or readback.get("target") != target
            or readback.get("status") not in {"delivered", "replied"} or readback.get("error")):
        raise RuntimeError(f"Handoff receipt {receipt_id} is not confirmed at {target}: {readback.get('status')}")
    handoff_task = sent.get("task")
    handoff_task_id = handoff_task.get("id") if isinstance(handoff_task, dict) else sent.get("task_id")
    if item["kind"] != "task" and handoff_task_id:
        # The handoff's default result recipient is the retiring source. Move
        # that assignment's reporting route too, without generating another brief.
        await cli(runner, source, "tasks", "--json", "assign", str(handoff_task_id),
                  "--to", target, "--report-to", target)
    return {"receipt_id": receipt_id, "address": target, "destination": dest}


async def transfer_control(runner, source, state, item, dest):
    """Move both control rows and their replay proof in one store transaction.

    Continuation migration is best-effort and rewrites writer-owned tokens. A
    retirement needs an atomic, snapshot-checked move, not token-stripped equality
    with an unrelated destination goal that happens to have the same text.
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
        if proof:
            if proof == {"source": old, "destination": new, "session_id": dest["session_id"]}:
                return proof
            raise RuntimeError(f"Transferred {kind} changed before retirement recovery")
        if new is not None or not old or json.loads(old) != item["record"]:
            raise RuntimeError(f"Cannot transfer {kind}: destination already owns one or the source changed")
        if kind == "loop":
            current_loop, retired_loop = LoopState.from_json(old), LoopState.from_json(old)
            retired_loop.status = "cleared"
            retired_loop.awaiting_response = False
            save_loop(dest["session_id"], current_loop, cursor=conn)
            save_loop(state["session_id"], retired_loop, cursor=conn)
        else:
            current_goal, retired_goal = GoalState.from_json(old), GoalState.from_json(old)
            retired_goal.status = "cleared"
            save_goal(dest["session_id"], current_goal, cursor=conn)
            save_goal(state["session_id"], retired_goal, cursor=conn)
        proof = {"source": read(conn, old_key),
                 "destination": read(conn, new_key), "session_id": dest["session_id"]}
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


async def prepare(runner, source, state):
    # Reconcile receipts independently of source inventory. A crash after moving
    # an item must not forget a still-unconfirmed handoff just because it moved.
    for item_id, handoff in state["handoffs"].items():
        if handoff.get("receipt_id") and handoff.get("moved"):
            continue
        item, dest = handoff["item"], handoff["destination"]
        sent = await receipt(runner, source, state, item, dest)
        handoff.update(sent)
        await save(runner, source, state)
        if not handoff.get("moved"):
            await transfer(runner, source, state, item, dest)
            handoff["moved"] = True
            await save(runner, source, state)

    items = await inventory(runner, source, state)
    owners = state.setdefault("owners", {})
    questions, selected = [], []
    for item in items:
        if item["id"] in state["handoffs"]:
            raise RuntimeError(f"Previously handed-off work still targets this topic: {item['id']}")
        selector = state.get("target") or owners.get(item["id"])
        if not selector:
            # Use the same exact-subject matcher as named Telegram delivery. A
            # title overlap or recency is never enough to silently pick an owner.
            try:
                dest = await destination(runner, source, item["title"])
            except ValueError:
                questions.append({"id": item["id"], "title": item["title"][:150],
                                  "suggestion": item["title"][:80]})
                continue
        else:
            dest = await destination(runner, source, selector)
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
    if questions:
        if not state.get("questions"):
            state["questions"] = questions
            state["phase"] = "waiting"
            await save(runner, source, state)
            lines = ["Where should this open work go? Reply with each number and its live topic name."]
            lines.extend(f"{n}. {q['title']} (suggested: {q['suggestion']})" for n, q in enumerate(questions, 1))
            from gateway.telegram_retire import confirmed
            sent = await runner._delivery_adapter_for(source).send(
                str(source.chat_id), "\n".join(lines), metadata={"thread_id": str(source.thread_id)})
            if not confirmed(sent, source):
                state.pop("questions", None)
                await save(runner, source, state)
                raise RuntimeError("Owner question was not delivered")
        return False
    # Repeating discovery catches work created by a final tool call and stale
    # destinations. Nothing can be deleted on an incomplete/failed inventory.
    if await inventory(runner, source, state):
        raise RuntimeError("Open work still points at this topic after handoff")
    return True
