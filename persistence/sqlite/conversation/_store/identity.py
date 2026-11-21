"""Durable message-identity columns for SessionDB (schema v31): the codec between the live identity keys
(``agent.message_metadata``) and the ``message_uid`` / ``absorbed_message_uids`` / ``tool_call_uids`` /
``tool_call_uid`` columns, shared by the transcript writers, restore, and transcript repair."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple, TypeVar

from agent.message_metadata import ABSORBED_MESSAGE_UIDS, MESSAGE_UID, TOOL_CALL_UID, TOOL_CALL_UIDS, stamp_message_uid, uid_list
from agent.message_sanitization import coalesce_tool_call_id
from hermes_state_common import _json_or, _placeholders


_MessageRow = TypeVar("_MessageRow", dict[str, Any], sqlite3.Row)


def _compaction_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in messages:
        stamp_message_uid(message)
    return _newest_message_uid_rows(messages)


def _newest_message_uid_rows(rows: Sequence[_MessageRow]) -> list[_MessageRow]:
    """Keep the last position of each durable uid. Unidentified legacy rows remain distinct.

    Input order is transcript order (stored rows are ordered by id), not timestamp order.
    This is logical identity only, never content or platform-message-id deduplication.
    """
    seen: set[str] = set()
    kept = []
    for row in reversed(rows):
        uid = row[MESSAGE_UID] if MESSAGE_UID in row.keys() else None
        if isinstance(uid, str) and uid:
            if uid in seen:
                continue
            seen.add(uid)
        kept.append(row)
    return kept[::-1]


def _carried_tail_slots(
    conn: sqlite3.Connection, tail_ids: list[int], messages: list[dict[str, Any]], tail_count: int,
) -> int:
    """Tail slots whose originals are ABOVE the watermark, never positional rewinds below it."""
    if not tail_ids or tail_count <= 0:
        return 0
    uids = {row[0] for row in conn.execute(
        f"SELECT message_uid FROM messages WHERE id IN ({_placeholders(tail_ids)})", tail_ids) if row[0]}
    return sum(1 for message in messages[-tail_count:]
               if isinstance(message.get(MESSAGE_UID), str) and message[MESSAGE_UID] in uids)


def _unrepresented_tail_rows(
    conn: sqlite3.Connection, tail_ids: list[int], session_id: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Newest tail generations not already inserted into the target by the compaction handoff.

    The handoff wins an overlap: it can carry an intentional prune of that logical message.
    The caller has already archived the source rows, except when cloning into a rotation child.
    """
    rows = conn.execute(
        f"SELECT id, session_id, message_uid, tool_calls FROM messages "
        f"WHERE id IN ({_placeholders(tail_ids)}) ORDER BY id", tail_ids).fetchall()
    targets = {session_id or row["session_id"] for row in rows}
    represented = {(row["session_id"], row[MESSAGE_UID]) for row in conn.execute(
        f"SELECT session_id, message_uid FROM messages WHERE session_id IN ({_placeholders(targets)}) "
        "AND active = 1", tuple(targets)).fetchall()} if targets else set()
    return [row for row in _newest_message_uid_rows(rows)
            if not row[MESSAGE_UID] or (session_id or row["session_id"], row[MESSAGE_UID]) not in represented]


def _live_or_column(msg: Mapping[str, Any], live_key: str, column: str) -> Any:
    """Flushed dicts and batch rows carry the live key; import payloads carry the column name."""
    return msg.get(live_key) if live_key in msg else msg.get(column)


def _uid_list(value: Any) -> list[str]:
    """Normalize a uid list (a live list, or the JSON text an export/import carries) to unique non-empty
    strings in order; anything else is ``[]``."""
    if isinstance(value, str):
        value = _json_or(value, [], "Failed to deserialize a message uid list, falling back to []")
    return uid_list(value)


def _uid_map(value: Any) -> dict[str, Any]:
    """Normalize a ``{tool call id: uid}`` map (a live dict, or the JSON text an export/import carries): a
    non-empty string uid, or a list of them for a provider id repeated inside one row (one per occurrence,
    see ``merge_tool_call_uids``); anything else is dropped, and a non-map is ``{}``."""
    if isinstance(value, str):
        value = _json_or(value, {}, "Failed to deserialize a tool-call uid map, falling back to {}")
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and k and (
        (isinstance(v, str) and v) or (isinstance(v, list) and v and all(isinstance(u, str) and u for u in v)))}


def _tool_call_uid_map(msg: Mapping[str, Any]) -> dict[str, Any]:
    return _uid_map(_live_or_column(msg, TOOL_CALL_UIDS, "tool_call_uids"))


def _absorbed_uids_json(msg: Mapping[str, Any]) -> Optional[str]:
    uids = _uid_list(_live_or_column(msg, ABSORBED_MESSAGE_UIDS, "absorbed_message_uids"))
    return json.dumps(uids) if uids else None


def _tool_call_uids_json(msg: Mapping[str, Any]) -> Optional[str]:
    uids = _tool_call_uid_map(msg)
    return json.dumps(uids, sort_keys=True) if uids else None


def _tool_call_uid_or_none(msg: Mapping[str, Any]) -> Optional[str]:
    uid = _live_or_column(msg, TOOL_CALL_UID, "tool_call_uid")
    return uid if isinstance(uid, str) and uid else None


def _live_extends(live_uid: Any, stored_uid: Any) -> bool:
    """A live per-occurrence list that starts with every stored occurrence: a fold's union not yet written."""
    stored = stored_uid if isinstance(stored_uid, list) else [stored_uid]
    return isinstance(live_uid, list) and live_uid[:len(stored)] == stored


def _fill_missing_tool_call_uids(row: Any, msg: MutableMapping[str, Any]) -> None:
    """Before a row-addressed rewrite serializes *msg*: stored uids for calls it still names but whose uid it
    lost (a clone), so the rewrite does not null them. A call the live dict dropped keeps no uid."""
    if not row["tool_call_uids"] or not isinstance(calls := msg.get("tool_calls"), list):
        return
    live = msg.get(TOOL_CALL_UIDS)
    live = live if isinstance(live, dict) else {}
    named = {coalesce_tool_call_id(tc) for tc in calls}
    if missing := {k: v for k, v in _uid_map(row["tool_call_uids"]).items() if k in named and k not in live}:
        msg[TOOL_CALL_UIDS] = {**live, **missing}


def _restore_row_identity(row: Any, msg: MutableMapping[str, Any]) -> None:
    """A stored row's uid and tool-call uids onto its live dict (the stored value wins). A live per-occurrence
    list that already holds the stored uid is kept: it is a fold's union the row has not been rewritten
    with yet, and replacing it with the stored single uid would strip the later occurrence."""
    if row[MESSAGE_UID]:
        msg[MESSAGE_UID] = row[MESSAGE_UID]
    if row["tool_call_uids"] and (stored := _uid_map(row["tool_call_uids"])):
        live = msg.get(TOOL_CALL_UIDS)
        live = live if isinstance(live, dict) else {}
        msg[TOOL_CALL_UIDS] = {**live, **{call_id: uid for call_id, uid in stored.items()
                                          if not _live_extends(live.get(call_id), uid)}}
    if row["tool_call_uid"]:
        msg[TOOL_CALL_UID] = row["tool_call_uid"]


def _restore_identity_columns(row: Any, msg: MutableMapping[str, Any]) -> None:
    """The stored identity columns onto a restored dict under their live keys (NULL/empty add nothing)."""
    _restore_row_identity(row, msg)
    if row["absorbed_message_uids"] and (absorbed := _uid_list(row["absorbed_message_uids"])):
        msg[ABSORBED_MESSAGE_UIDS] = absorbed


def _stable_tool_key(row: Any) -> Optional[tuple[Any, ...]]:
    """Display-dedupe key of a tool-calling assistant row built from its stable call ids instead of the arguments a
    prune rewrites (#117750: a pruned carried-forward copy must collapse with its durable original). ``None`` for
    every other row and for an incomplete id set, so the caller keeps the full content key: a tool RESULT row keeps
    its payload in the key, because folding the archived full output into its pruned stub would drop the original
    from compacted history and transcript exports, and distinct id-less calls never merge."""
    if row["role"] != "assistant":
        return None
    calls = _json_or(row["tool_calls"] or "[]", [], "Failed to deserialize tool_calls, falling back to []")
    call_ids = tuple(coalesce_tool_call_id(tc) for tc in calls or ())
    if call_ids and all(call_ids):
        return ("assistant", None, row["timestamp"], row["tool_call_id"], call_ids, row["tool_name"])
    return None


# hashlib releases the GIL for inputs of 2,048 bytes or more; display_identity stays below it.
_GIL_HELD_HASH_CHUNK = 2047


def display_identity(key: tuple[Any, ...]) -> bytes:
    """sha256 of ``repr(key)``, fed in chunks below hashlib's GIL-release threshold.

    The digest is identical, but a large payload no longer drops the GIL and then waits a switch
    interval to take it back. A compaction plan hashes thousands of such rows (6,089 of 16,969 on
    a live session), which took 11.6 s behind one CPU-bound thread and 1.5 s chunked (2026-10-25).
    """
    data = repr(key).encode("utf-8", "surrogatepass")
    digest = hashlib.sha256()
    for start in range(0, len(data), _GIL_HELD_HASH_CHUNK):
        digest.update(data[start:start + _GIL_HELD_HASH_CHUNK])
    return digest.digest()
