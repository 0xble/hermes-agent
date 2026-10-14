"""A lease-less rewrite of a stale generation must not re-insert the replaced transcript.

Gateway hygiene compacts a session in place while the live turn keeps running on the transcript it
loaded before the commit. A lease-less rewrite from that turn (proactive tool-result prune,
micro-compaction) passes ``held_messages``: when the store already has a summary the held history does
not carry, the rewrite was built from the replaced generation, and committing it would archive the
summary and re-insert the whole pre-compaction transcript beside it. The commit refuses instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.context_compressor import StaleHeldHistory
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path: Path):
    handle = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield handle
    finally:
        handle.close()


def _active(db: SessionDB, sid: str):
    conn = sqlite3.connect(db.db_path)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT role, content, _compressed_summary FROM messages WHERE session_id = ? AND active = 1 "
            "ORDER BY id", (sid,)).fetchall()]
    finally:
        conn.close()


def _seed(db: SessionDB, sid: str, turns: int = 6):
    db.create_session(sid, source="telegram")
    for i in range(turns):
        db.append_message(sid, "user" if i % 2 == 0 else "assistant", f"m{i}", timestamp=1727000000.0 + i)


def test_stale_lease_less_rewrite_is_refused_after_a_summary_lands(db):
    sid = "s-stale"
    _seed(db, sid)
    stale = db.get_messages_as_conversation(sid, include_row_ids=True)  # the live turn's view
    # Hygiene commits a summary in place.
    db.archive_and_compact(sid, [{"role": "user", "content": "[summary]", "_compressed_summary": True},
                                 *db.get_messages_as_conversation(sid)[-2:]], tail_count=2)
    after_hygiene = _active(db, sid)
    assert after_hygiene[0] == ("user", "[summary]", 1)

    pruned = [dict(m, content=f"{m['content']}-pruned") for m in stale]
    with pytest.raises(StaleHeldHistory):
        db.archive_and_compact(sid, pruned, held_messages=stale)
    assert _active(db, sid) == after_hygiene  # nothing written


def test_current_generation_rewrite_still_commits(db):
    sid = "s-current"
    _seed(db, sid)
    db.archive_and_compact(sid, [{"role": "user", "content": "[summary]", "_compressed_summary": True},
                                 *db.get_messages_as_conversation(sid)[-2:]], tail_count=2)
    current = db.get_messages_as_conversation(sid, include_row_ids=True)  # carries the summary
    db.archive_and_compact(sid, [*current[:-1], {"role": "assistant", "content": "m5-pruned"}],
                           held_messages=current)
    assert [row[1] for row in _active(db, sid)] == ["[summary]", "m4", "m5-pruned"]


def test_no_summary_yet_is_never_stale(db):
    sid = "s-fresh"
    _seed(db, sid, turns=4)
    held = db.get_messages_as_conversation(sid, include_row_ids=True)
    db.archive_and_compact(sid, [*held[:-1], {"role": "assistant", "content": "m3-pruned"}], held_messages=held)
    assert [row[1] for row in _active(db, sid)] == ["m0", "m1", "m2", "m3-pruned"]


# Gateway shape: ``load_transcript`` hands the turn a history with no ``_row_id``s. It still carries the
# durable message UID and row snapshot, which must take precedence over content fallback.

def _gateway_load(db: SessionDB, sid: str):
    held = db.get_messages_as_conversation(sid, repair_alternation=True)
    assert all("_row_id" not in m for m in held)
    return held


def test_stale_rewrite_without_row_ids_is_refused(db):
    sid = "s-stale-gw"
    _seed(db, sid)
    stale = _gateway_load(db, sid)
    db.archive_and_compact(sid, [{"role": "user", "content": "[summary]", "_compressed_summary": True},
                                 *db.get_messages_as_conversation(sid)[-2:]], tail_count=2)
    after_hygiene = _active(db, sid)

    pruned = [dict(m, content=f"{m['content']}-pruned") for m in stale]
    with pytest.raises(StaleHeldHistory):
        db.archive_and_compact(sid, pruned, held_messages=stale)
    assert _active(db, sid) == after_hygiene


def test_identical_summary_from_new_generation_is_refused_without_row_ids(db):
    sid = "s-identical-summary-gw"
    _seed(db, sid)
    summary = {"role": "assistant", "content": "[same summary]", "_compressed_summary": True}
    db.archive_and_compact(sid, [summary, *db.get_messages_as_conversation(sid)[-2:]], tail_count=2)
    stale = _gateway_load(db, sid)
    held_summary = next(message for message in stale if message.get("_compressed_summary"))
    assert "_row_id" not in held_summary
    assert held_summary.get("message_uid") and held_summary.get("_db_row_snapshot")

    # A later generation has byte-identical summary content but a different durable row identity.
    current_tail = db.get_messages_as_conversation(sid)[-2:]
    db.archive_and_compact(
        sid, [{"role": "assistant", "content": "[same summary]", "_compressed_summary": True}, *current_tail],
        tail_count=2,
    )
    after_winner = _active(db, sid)
    assert sum(row[2] for row in after_winner) == 1

    # Preserve the old summary text so content-only matching would incorrectly admit this stale rewrite.
    stale_rewrite = [
        dict(message, content=(message["content"] if message.get("_compressed_summary") else f"{message['content']}-stale"))
        for message in stale
    ]
    with pytest.raises(StaleHeldHistory):
        db.archive_and_compact(sid, stale_rewrite, held_messages=stale)
    assert _active(db, sid) == after_winner


def test_current_generation_rewrite_without_row_ids_commits(db):
    sid = "s-current-gw"
    _seed(db, sid)
    db.archive_and_compact(sid, [{"role": "user", "content": "[summary]", "_compressed_summary": True},
                                 *db.get_messages_as_conversation(sid)[-2:]], tail_count=2)
    current = _gateway_load(db, sid)
    db.archive_and_compact(sid, [*current[:-1], {"role": "assistant", "content": "m5-pruned"}],
                           held_messages=current)
    assert [row[1] for row in _active(db, sid)] == ["[summary]", "m4", "m5-pruned"]


def test_rewritten_summary_is_judged_by_the_held_snapshot_not_the_rewrite(db):
    """A rewrite of the summary itself (micro-compaction defrag) is the current generation as long as the
    held history is the snapshot taken at load, not the live dicts the rewrite edited."""
    sid = "s-defrag-gw"
    _seed(db, sid)
    db.archive_and_compact(sid, [{"role": "assistant", "content": "[summary v1]", "_compressed_summary": True},
                                 *db.get_messages_as_conversation(sid)[-2:]], tail_count=2)
    live = _gateway_load(db, sid)
    held = [dict(m) for m in live]
    live[0]["content"] = "[summary v2]"
    db.archive_and_compact(sid, live, held_messages=held)
    assert [row[1] for row in _active(db, sid)] == ["[summary v2]", "m4", "m5"]
