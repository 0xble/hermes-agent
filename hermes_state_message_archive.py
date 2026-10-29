"""Compaction's shared archive/rewind update phase."""

from hermes_state_common import _placeholders
from hermes_state_write_metrics import add_write_rows, write_phase


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
