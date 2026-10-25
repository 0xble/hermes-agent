"""Native-store and transport witnesses. Absence and ambiguity fail closed."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any

KINDS = ("long", "background", "delegation", "approval", "goal", "loop", "cron", "followup", "admission")
BAD = re.compile(r"(?i)(interrupted|unknown.*result|model.*error|provider.*error|unauthorized|\b401\b|cut.?off|shutting down)")


def text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def messages(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("messages", "data", "result"):
            if key in value:
                return messages(value[key])
    return []


def calls(rows):
    found = []
    for row in rows:
        raw = row.get("tool_calls") or []
        if isinstance(raw, str):
            raw = json.loads(raw)
        for call in raw:
            found.append({**call, "session_id": row.get("session_id")})
    return found


def contains(value, token, kind=None):
    raw = text(value)
    return token in raw and (kind is None or kind in raw)


def rows_for(snapshot, name, token):
    return [r for r in snapshot.get("tables", {}).get(name, []) if contains(r, token)]


def native_snapshot(home: Path, directory: Path, token: str, effects, peer: str):
    """Use read-only SQLite and native JSON stores, never an agent's claimed success."""
    tables = dict(effects.read_state_db(home).get("tables", {}))
    # SQLite messages are the canonical durable gateway transcript. JSONL is
    # only a fallback, not an extra source the healthy DB path must manufacture.
    transcripts = list(tables.get("messages", []))
    for path in (home / "sessions").glob("*.jsonl"):
        if transcripts:
            break
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if contains(row, token):
                transcripts.append(row)
    def load(path, default):
        return json.loads(path.read_text()) if path.is_file() else default
    events = {kind: [json.loads(line) for line in (directory / f"{token}-{kind}.jsonl").read_text().splitlines()]
              if (directory / f"{token}-{kind}.jsonl").is_file() else [] for kind in KINDS}
    from gateway.control_socket import query_gateway_control
    query = effects.query_control or query_gateway_control
    owners = []
    for slot in ("a", "b"):
        identity = query(home, "identify", slot=slot)
        if identity:
            owners.append({**identity, "status": query(home, "status", slot=slot),
                           "handover": query(home, "handover-status", slot=slot)})
    results = [json.loads(path.read_text()) for path in (home / "logs/process-results").glob("proc_*.json")]
    executions = []
    execution_path = home / "cron/executions.db"
    if execution_path.is_file():
        with sqlite3.connect(execution_path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            executions = [dict(row) for row in db.execute("SELECT * FROM executions")]
    import psutil
    from gateway.status import get_process_start_time
    for owner in owners:
        owner["kernel_start_time"] = get_process_start_time(owner["pid"])
        marker = home / "releases" / str(owner.get("code_sha")) / ".hermes_build_sha"
        owner["manifest_sha"] = marker.read_text().strip() if marker.is_file() else None
    living = {}
    for event in [e for group in events.values() for e in group if e.get("phase") == "start"]:
        try:
            p = psutil.Process(event["pid"])
            living[str(p.pid)] = p.create_time() if p.is_running() else None
        except psutil.Error:
            pass
    return {"tables": tables, "transcripts": transcripts, "events": events,
            "processes": load(home / "processes.json", []),
            "cron": load(home / "cron" / "jobs.json", {}), "owners": owners,
            "living": living, "process_results": results, "executions": executions,
            "chat": effects.read_chat(peer, 500)}


def read_db(home):
    path = home / "state.db"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {"tables": {name: [dict(r) for r in db.execute(f'SELECT * FROM "{name}"')]
                           for name in ("messages", "gateway_routing", "async_delegations", "state_meta", "delivery_obligations") if name in names}}


def active_owner(snapshot, sha, home):
    owners = [o for o in snapshot.get("owners", []) if o.get("standby") is False and not (o.get("handover") or {}).get("token_lock_released")]
    if len(owners) != 1:
        raise ValueError("not exactly 1 serving control-socket owner")
    owner = owners[0]
    if (owner.get("code_sha") != sha or not owner.get("pid") or not owner.get("start_time")
            or Path(owner.get("hermes_home", "")).resolve() != home.resolve()
            or owner.get("supervisor") != "launchd" or not owner.get("status")
            or owner.get("kernel_start_time") != owner["start_time"]
            or owner.get("manifest_sha") != sha):
        raise ValueError("wrong or incomplete native process/release identity")
    return owner


def event_pair(snapshot, token, kind, final=False):
    events = snapshot.get("events", {}).get(kind, [])
    starts = [e for e in events if e.get("token") == token and e.get("phase") == "start"]
    finishes = [e for e in events if e.get("token") == token and e.get("phase") == "finish"]
    if len(starts) != 1 or len(finishes) != int(final):
        return False
    start = starts[0]
    if final:
        finish = finishes[0]
        return (start.get("pid") == finish.get("pid") and start.get("created") == finish.get("created")
                and finish["monotonic"] - start["monotonic"] >= (150 if kind == "long" else 0)
                and finish["at"] >= start["at"])
    return snapshot.get("living", {}).get(str(start.get("pid"))) == start.get("created")


def tool_witness(snapshot, token, kind, final=False):
    rows = snapshot.get("tables", {}).get("messages", [])
    matching = [c for c in calls(rows) if contains(c, token, f"--kind {kind}")
                and c.get("function", {}).get("name", "").split(".")[-1] == "terminal"]
    # CLI arguments may be quoted in JSON, but the worker command itself is literal.
    if len(matching) != 1 or not matching[0].get("id"):
        return False
    call = matching[0]
    results = [r for r in rows if r.get("role") == "tool" and r.get("tool_call_id") == call["id"]]
    if kind == "background" and final:
        receipts = [r for r in snapshot.get("process_results", []) if f"EFFECT {token} background" in str(r.get("output", ""))]
        return (len(results) == 1 and len(receipts) == 1 and receipts[0].get("exit_code") == 0
                and str(receipts[0].get("id", "missing")) in text(results[0]))
    return (len(results) == int(final) and
            (not final or f"EFFECT {token} {kind}" in text(results[0])) and
            any(contains(row, token, f"--kind {kind}") for row in snapshot.get("transcripts", [])))


def armed(snapshot, token, owner):
    """Every mixed-work category must have a native witness at the same boundary."""
    rows = snapshot.get("tables", {}).get("messages", [])
    pending_calls = [c for c in calls(rows) if contains(c, token, "--kind approval")
                     and c.get("function", {}).get("name", "").split(".")[-1] == "terminal"]
    approval_messages = [m for m in messages(snapshot.get("chat")) if contains(m, token, "approval")
                         and m.get("sender_id") == 8899335561 and (m.get("reply_markup") or
                         re.search(r"(?i)(approval required|/approve|approve.*deny)", str(m.get("text", ""))))]
    delegates = rows_for(snapshot, "async_delegations", token)
    metadata = rows_for(snapshot, "state_meta", token)
    work_sessions = {c.get("session_id") for c in calls(rows) if contains(c, token, "--kind long") and c.get("function", {}).get("name", "").split(".")[-1] == "terminal"}
    def meta(prefix):
        return any(r.get("key") in {prefix + str(sid) for sid in work_sessions} and
                   json.loads(r["value"]).get("status") == "active" for r in metadata)
    processes = snapshot.get("processes", [])
    if isinstance(processes, dict):
        processes = list(processes.values())
    bg = [p for p in processes if contains(p, token, "--kind background")]
    cron = snapshot.get("cron", {})
    jobs = cron if isinstance(cron, list) else cron.get("jobs", [])
    return {
        "tool150": event_pair(snapshot, token, "long") and tool_witness(snapshot, token, "long")
                   and snapshot["events"]["long"][0].get("seconds", 0) >= 150,
        "background": event_pair(snapshot, token, "background") and len(bg) == 1
                      and bg[0].get("registry_owner_pid") == owner["pid"]
                      and bg[0].get("registry_owner_started_at") == owner["start_time"],
        "delegation": event_pair(snapshot, token, "delegation") and len(delegates) == 1
                      and delegates[0].get("state") == "running" and delegates[0].get("owner_pid") == owner["pid"]
                      and delegates[0].get("owner_started_at") == owner["start_time"],
        "approval": len(pending_calls) == 1 and bool(approval_messages)
                    and not snapshot.get("events", {}).get("approval")
                    and not any(r.get("tool_call_id") == pending_calls[0].get("id") for r in rows),
        "goal": meta("goal:"), "loop": meta("loop:"),
        "cron": any(contains(j, token) and (j.get("enabled") is True or j.get("run_claim")) for j in jobs),
        "owner_chain": all(any(a.get("pid") == owner["pid"] and round(a.get("created", 0) * 100) == owner["start_time"] for a in snapshot["events"][kind][0].get("ancestors", []))
                           for kind in ("long", "background", "delegation") if snapshot.get("events", {}).get(kind)),
    }


def completed(snapshot, token, owner, successor, sends, *, db_sends=None):
    rows = snapshot.get("tables", {}).get("messages", [])
    observed = messages(snapshot.get("chat"))
    checks = {}
    for kind in KINDS:
        checks[f"effect:{kind}"] = event_pair(snapshot, token, kind, final=True)
        # Independent native call/result and transcript evidence, not an ACK.
        checks[f"tool:{kind}"] = tool_witness(snapshot, token, kind, final=True)
        final = f"RESULT {token} {kind}"
        checks[f"telegram:{kind}"] = sum(str(m.get("text", "")).count(final) for m in observed if m.get("sender_id") == 8899335561) == 1
        checks[f"db:{kind}"] = sum(str(r.get("content", "")).count(final) for r in rows if r.get("role") == "assistant" and not r.get("tool_calls")) == 1
    metadata = rows_for(snapshot, "state_meta", token)
    work_sessions = {c.get("session_id") for c in calls(rows) if contains(c, token, "--kind long") and c.get("function", {}).get("name", "").split(".")[-1] == "terminal"}
    checks["goal_settled"] = any(r.get("key") in {"goal:" + str(sid) for sid in work_sessions} and json.loads(r["value"]).get("status") in {"done", "cleared"} for r in metadata)
    checks["loop_once"] = any(r.get("key") in {"loop:" + str(sid) for sid in work_sessions} and json.loads(r["value"]).get("ticks_fired") == 1 and json.loads(r["value"]).get("status") == "done" for r in metadata)
    executions = [r for r in snapshot.get("executions", []) if contains(r.get("output"), token)]
    checks["cron_once"] = len(executions) == 1 and executions[0].get("status") == "completed" and executions[0].get("code_sha") == successor["code_sha"] and executions[0].get("delivery_status") == "delivered"
    checks["inbound"] = all(sum(str(m.get("id")) == str(mid) and m.get("outgoing") is True
                                        for m in observed) == 1 for mid in sends)
    checks["db_inbound"] = all(sum(str(r.get("platform_message_id")) == str(mid)
                                           and r.get("role") == "user" for r in rows) == 1 for mid in (sends if db_sends is None else db_sends))
    delegates = rows_for(snapshot, "async_delegations", token)
    checks["delegation_delivery"] = len(delegates) == 1 and delegates[0].get("state") == "completed" and delegates[0].get("delivery_state") == "delivered" and delegates[0].get("delivery_attempts") == 1
    obligations = rows_for(snapshot, "delivery_obligations", token)
    checks["delivery_ledger"] = (bool(obligations) and all(r.get("state") == "delivered" and r.get("attempts") == 0 for r in obligations)
                                 and all(sum(str(r.get("content", "")).count(f"RESULT {token} {kind}") for r in obligations) == 1 for kind in KINDS if kind != "cron"))
    sessions = {r.get("session_id") for r in rows if contains(r, token) and r.get("session_id")}
    checks["no_cut_or_provider_error"] = not any(BAD.search(str(r.get("content", "")))
                                                for r in rows if (contains(r, token) or r.get("session_id") in sessions) and r.get("role") in {"tool", "assistant"})
    boundary = min((int(mid) for mid in sends), default=0)
    checks["no_telegram_cut"] = not any(BAD.search(str(m.get("text", ""))) for m in observed
                                         if m.get("sender_id") == 8899335561 and int(m.get("id", 0)) >= boundary)
    long_end = [e["at"] for e in snapshot.get("events", {}).get("long", []) if e.get("phase") == "finish"]
    admission = [e["at"] for e in snapshot.get("events", {}).get("admission", []) if e.get("phase") == "start"]
    followup = [e["at"] for e in snapshot.get("events", {}).get("followup", []) if e.get("phase") == "start"]
    checks["admission_during_predecessor_work"] = len(long_end) == len(admission) == 1 and admission[0] < long_end[0]
    checks["queued_after_original_tool"] = len(long_end) == len(followup) == 1 and followup[0] >= long_end[0]
    for kind, expected in (("long", owner), ("background", owner), ("delegation", owner), ("admission", successor)):
        starts = [e for e in snapshot.get("events", {}).get(kind, []) if e.get("phase") == "start"]
        checks[f"owner:{kind}"] = len(starts) == 1 and any(a.get("pid") == expected["pid"] and round(a.get("created", 0) * 100) == expected["start_time"] for a in starts[0].get("ancestors", []))
    return checks


def evidence(snapshot, token):
    import hashlib
    tables = snapshot.get("tables", {})
    messages_ = tables.get("messages", [])
    sessions = {r.get("session_id") for r in messages_ if contains(r, token)}
    def receipt(row):
        keys = ("id", "session_id", "tool_call_id", "role", "key", "delegation_id", "obligation_id", "job_id", "state", "status", "owner_pid")
        return {**{key: row[key] for key in keys if key in row},
                "sha256": hashlib.sha256(text(row).encode()).hexdigest()}
    return {"owners": snapshot.get("owners", []),
            "db_rows": {name: [receipt(r) for r in rows if contains(r, token) or (name == "messages" and r.get("session_id") in sessions)]
                        for name, rows in tables.items()},
            "telegram_ids": [m.get("id") for m in messages(snapshot.get("chat")) if contains(m, token)],
            "process_receipts": [receipt(r) for r in snapshot.get("process_results", []) if contains(r, token)],
            "cron_executions": [receipt(r) for r in snapshot.get("executions", []) if contains(r, token)]}


def restart_tool_executed(snapshot, token):
    rows = snapshot.get("tables", {}).get("messages", [])
    matching = [c for c in calls(rows) if c.get("function", {}).get("name", "").endswith("request_restart") and contains(c, token)]
    if len(matching) != 1:
        return False
    results = [r for r in rows if r.get("role") == "tool" and r.get("tool_call_id") == matching[0].get("id")]
    if len(results) != 1:
        return False
    payload = results[0].get("content")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return False
    return isinstance(payload, dict) and payload.get("success") is True and payload.get("routed") is True
