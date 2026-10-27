"""Compaction's shared archive/rewind update phase."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hermes_state_common import _placeholders
from hermes_state_identity import _carried_tail_slots, _compaction_messages, _unrepresented_tail_rows
from hermes_state_write_metrics import add_write_rows, write_phase

if TYPE_CHECKING:
    from hermes_state_display_fold import _AffectedFold, _FoldPlan


_ARCHIVE_ACTIVE_SQL = "UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1"


def archive_message_rows(conn, session_id: str, rewind_ids: list[int], *, exclude_rewound: bool = False) -> None:
    """Keep rewound rows out of display history and archive the remaining active generation."""
    with write_phase("archive_mark_inactive"):
        archive_sql, archive_params = _ARCHIVE_ACTIVE_SQL, [session_id]
        if rewind_ids:
            placeholders = _placeholders(rewind_ids)
            cursor = conn.execute(
                "UPDATE messages SET active = 0, compacted = 0 "
                f"WHERE session_id = ? AND id IN ({placeholders})", [session_id, *rewind_ids])
            add_write_rows("rewound", cursor.rowcount)
            if exclude_rewound:
                archive_sql += f" AND id NOT IN ({placeholders})"
                archive_params += rewind_ids
        cursor = conn.execute(archive_sql, archive_params)
        add_write_rows("archived", cursor.rowcount)


def clone_tail_survivors(db, conn, tail_ids: list[int], *, session_id: str | None = None) -> tuple[int, int]:
    """Clone the concurrent tail's newest generation per ``message_uid`` that the compaction handoff did
    not already insert into the target, returning ``(cloned, tool_calls)`` for the session counters."""
    from hermes_state_messages import _tool_calls_len

    rows = _unrepresented_tail_rows(conn, tail_ids, session_id) if tail_ids else []
    if not rows:
        return 0, 0
    db._clone_message_rows(conn, [int(row["id"]) for row in rows], session_id=session_id)
    return len(rows), sum(_tool_calls_len(row["tool_calls"]) for row in rows)


def archive_named_rows(
    db, conn, session_id: str, compacted_messages: list[dict[str, Any]], covered: list[int], *,
    tail_count: int, carried_messages: list[dict[str, Any]] | None, patched_model_config: Any,
    patch: bool, merged_away: set[int],
    fold_plan: _FoldPlan | _AffectedFold | None = None,
) -> int:
    """Archive *covered* as summarized and clone every other active row after the new set.

    A gap below the newest held id, and rows appended after an unpersisted turn, are not
    in *covered*. They take the concurrent-append path: rewind the original, insert the
    compacted transcript, then clone them so they stay live and searchable once each.
    """
    from hermes_state_messages import _ACTIVE_IDS_SQL, _SET_COUNTERS_SQL

    with write_phase("archive_enumerate"):
        active_ids = [int(row["id"]) for row in conn.execute(_ACTIVE_IDS_SQL, (session_id,)).fetchall()]
        covered_set = set(covered)
        carried_ids = db._resolve_carried_row_ids(conn, session_id, carried_messages or [])
        covered_set.update(carried_ids)
        unseen = [row_id for row_id in active_ids if row_id not in covered_set]
        covered_active = [row_id for row_id in active_ids if row_id in covered_set]
        rewind_ids = list(carried_ids)
        # Tail slots carried from unseen (concurrent) rows never consume a positional rewind below them.
        tail_below = tail_count - _carried_tail_slots(conn, unseen, compacted_messages, tail_count)
        if tail_below > 0:
            rewind_ids += db._tail_originals(covered_active, tail_below, merged_away)
        rewind_ids += unseen
        rewind_ids = list(dict.fromkeys(rewind_ids))
    db._seed_display_removals(conn, session_id, fold_plan, rewind_ids)
    archive_message_rows(conn, session_id, rewind_ids)
    # Same identity carry as publish_compression_child (#59661): carried tail copies keep the
    # durable row's timestamp so display dedupe still collapses generations. Donors are active-only;
    # the rewound/archived originals above never donate, so this is a no-op for a clean compaction.
    with write_phase("archive_timestamp_carry"):
        db._carry_parent_timestamps(conn, session_id, compacted_messages)
    inserted, tool_calls_total = db._insert_message_rows(conn, session_id, _compaction_messages(compacted_messages))
    if unseen:
        cloned, unseen_tool_calls = clone_tail_survivors(db, conn, unseen)
        inserted += cloned
        tool_calls_total += unseen_tool_calls
    # A carried copy whose stored identity was computed differently lands in its own
    # display_order group and would project twice; re-fold before publishing (#122167).
    db._reconcile_display_orders(conn, session_id, fold_plan)
    with write_phase("session_counters"):
        conn.execute(
            f"{_SET_COUNTERS_SQL}{', model_config = ?' if patch else ''} WHERE id = ?",
            (inserted, tool_calls_total, *((patched_model_config,) if patch else ()), session_id))
    return inserted
