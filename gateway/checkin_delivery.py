"""Supervisor delivery provenance on the existing transcript and transport payloads."""
from __future__ import annotations

import json

from hermes_cli.checkins import CheckinConfig, DISPLAY_KIND

DISARMED = "supervisor_checkin_disarmed"


def transcript_metadata(event):
    from gateway.run_checkins import descriptor
    tick = descriptor(event)
    token = getattr(event, "_gateway_active_turn_token", None)
    if not tick or not token:
        return {}
    metadata = {"checkin_turn_id": token, "checkin_root": tick[0]}
    if hasattr(event, "_checkin_pending_root"):
        metadata["checkin_pending_root"] = event._checkin_pending_root
    return metadata


def delivery_metadata(event, metadata):
    root = getattr(event, "_checkin_pending_root", None)
    if root is None:
        return metadata
    return {**(metadata or {}), "_checkin_scope": {"root": root, "source": event.source.to_dict()}}


def retain_policy(manager, event, root):
    """Keep the terminal owner beside its exact opener, including human takeovers.

    This is transcript delivery metadata, not prompt content or another delivery ledger. A
    pending status retains the root, a human/completed result clears it. Compression and
    restart readers match the exact turn token rather than a newer turn on the same route.
    """
    event._checkin_pending_root = root
    from gateway.session_results import execution_result
    captured = execution_result.get()
    if captured is not None:
        captured.setdefault("result", {})["checkin_pending_root"] = root
    token = getattr(event, "_gateway_active_turn_token", None)
    if not token:
        return

    def write(conn):
        from gateway.run_checkins import descriptor
        lineage = list(dict.fromkeys([*manager.lineage, descriptor(event)[0]]))
        placeholders = ",".join("?" for _ in lineage)
        row = conn.execute(
            f"SELECT id, display_metadata FROM messages WHERE session_id IN ({placeholders}) "
            "AND role='user' AND display_kind=? AND json_valid(display_metadata) "
            "AND json_extract(display_metadata, '$.checkin_turn_id')=? ORDER BY id DESC LIMIT 1",
            (*lineage, DISPLAY_KIND, token)).fetchone()
        if row is not None:
            metadata = {**json.loads(row[1]), "checkin_pending_root": root}
            conn.execute("UPDATE messages SET display_metadata=? WHERE id=?", (json.dumps(metadata), row[0]))
    manager.db._execute_write(write)


def pending_root(runner, route, token):
    if not token:
        return None
    store = getattr(runner, "session_store", None)
    if not callable(getattr(store, "_db_for_key", None)):
        return None
    db = store._db_for_key(route)
    if db is None:
        raise OSError("Supervisor provenance store is unavailable")
    entry = store.lookup_by_session_key(route)
    lineage = db.get_compression_lineage(entry.session_id) if entry is not None else []
    placeholders = ",".join("?" for _ in lineage) or "NULL"
    # The token is unique to this exact turn. Do not look only in the current route's
    # lineage: a reset may have moved it while the original delivery was pending.
    rows = db._read_all(
        "SELECT display_metadata FROM messages WHERE session_id IN "
        f"(SELECT id FROM sessions WHERE session_key=? OR id IN ({placeholders})) "
        "AND role='user' AND display_kind=? AND json_valid(display_metadata) "
        "AND json_extract(display_metadata, '$.checkin_turn_id')=? ORDER BY id DESC LIMIT 1",
        (route, *lineage, DISPLAY_KIND, token))
    if not rows:
        return None
    metadata = json.loads(rows[0][0])
    # A crash before terminal shaping still owes only a supervisor status. After shaping,
    # explicit null records a human takeover or the permitted completed result.
    return metadata.get("checkin_pending_root", metadata["checkin_root"])


async def permits_delivery(runner, root, source):
    if root is None:
        return True
    scope = runner._async_profile_scope_for_source(source)
    if scope is None:
        return False
    async with scope:
        config = await runner._run_in_executor_with_context(CheckinConfig.load)
        return config.permits(root)


async def permits_metadata(runner, metadata, *, retry_on_read_error=False):
    policy = (metadata or {}).get("_checkin_scope")
    if policy is None:
        return True
    if policy["source"] is None:
        return False
    from gateway.session import SessionSource
    try:
        return await permits_delivery(runner, policy["root"], SessionSource.from_dict(policy["source"]))
    except OSError:
        if retry_on_read_error:
            raise
        return False


async def recovery_metadata(runner, route, token):
    if not token:
        return {}
    root = await runner._run_in_executor_with_context(lambda: pending_root(runner, route, token))
    if root is None:
        return {}
    entry = await runner._run_in_executor_with_context(runner.session_store.lookup_by_session_key, route)
    source = runner._restored_source(entry) if entry is not None else None
    return {"_checkin_scope": {"root": root, "source": source.to_dict() if source is not None else None}}


async def permits_recovery(runner, route, token):
    return await permits_metadata(runner, await recovery_metadata(runner, route, token), retry_on_read_error=True)


def suppress_result(result):
    result.update(final_response="", delivery_text="", _model_final_response="",
                  intentional_silence=True, already_sent=False)
    from gateway.session_results import execution_result
    captured = execution_result.get()
    if captured is not None:
        captured["result"] = {**(captured.get("result") or {}), **result}
    return result


async def fence_local_settlement(authority, live, row, response, captured):
    descriptor = row["payload"].get("local_automation_v1", {})
    result = (captured or {}).get("result", {})
    if descriptor.get("display_kind") != DISPLAY_KIND or "checkin_pending_root" not in result:
        return response
    try:
        permitted = await permits_delivery(authority.runner, result["checkin_pending_root"], live.source)
    except OSError:
        permitted = False
    if permitted:
        return response
    suppress_result(result)
    return ""
