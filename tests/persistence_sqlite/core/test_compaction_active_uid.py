"""Compaction handoffs and concurrent clones must name each live message once."""

from copy import deepcopy

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    store.create_session("parent", source="test")
    yield store
    store.close()


def _summary():
    return [{"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
             "_compressed_summary": True}]


def _tail():
    return [
        {"role": "user", "content": "background completion", "message_uid": "user-uid"},
        {"role": "assistant", "content": "", "message_uid": "call-uid",
         "tool_calls": [{"id": "call-1", "type": "function",
                         "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "content": "completed", "message_uid": "result-uid",
         "tool_call_id": "call-1", "tool_name": "terminal", "api_content": "verbatim result"},
        {"role": "assistant", "content": "finished", "message_uid": "reply-uid"},
    ]


def _duplicate_active_tail(db, rows):
    # Historical corruption fixture. Bypass repair/upsert, just like the old SQL cloner did.
    ids = [row["_row_id"] for row in rows]
    def duplicate(conn):
        columns = ", ".join(c for c in db._message_column_names(conn) if c != "id")
        conn.execute(f"INSERT INTO messages ({columns}) SELECT {columns} FROM messages "
                     f"WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id", ids)
    db._execute_write(duplicate)
    db._write_sql("UPDATE messages SET content = 'newest reply' WHERE id = "
                  "(SELECT MAX(id) FROM messages WHERE session_id = ?)", ("parent",))


def _commit(db, mode, handoff, watermark, covered, tail_count=0):
    if mode == "rotation":
        db.publish_compression_child(
            parent_session_id="parent", child_session_id="child", source="test",
            messages=handoff, watermark=watermark, require_compression_lease=False)
        return "child"
    assert db.try_acquire_compression_lock("parent", "late-worker")
    db.archive_and_compact(
        "parent", handoff, watermark=watermark, lock_holder="late-worker", tail_count=tail_count,
        covered_ids=covered if mode == "coverage" else None)
    db.release_compression_lock("parent", "late-worker")
    return "parent"


def _assert_unique(db, sid, expected):
    live = db.get_messages(sid)
    uids = [row["message_uid"] for row in live]
    assert len(uids) == len(set(uids)), f"duplicate ACTIVE message_uid: {uids}"
    assert [row["content"] for row in live] == expected
    session = db.get_session(sid)
    assert session["message_count"] == len(live)
    assert session["tool_call_count"] == 1
    return live


@pytest.mark.parametrize("mode", ["watermark", "coverage", "rotation"])
def test_carried_handoff_overlaps_concurrent_tail(db, mode):
    """A late adoption carries a row appended after its start watermark."""
    db.append_message("parent", role="user", content="summarized")
    watermark = db.get_active_message_watermark("parent")
    covered = [watermark]
    if mode != "rotation":
        assert db.try_acquire_session_turn_lease("parent", "turn")
        assert db.try_acquire_compression_lock("parent", "late-worker")
        # Hygiene's hold expires but its worker keeps commit admission.
        db.release_session_turn_lease("parent", "turn")
    tail = _tail()
    db.append_messages_batch("parent", tail)
    before = db.get_messages("parent", include_inactive=True)
    # A protected tail included in the handoff ALSO qualifies as concurrent tail.
    handoff = _summary() + deepcopy(tail)
    handoff[-2]["content"] = handoff[-2]["api_content"] = "[pruned]"
    sid = _commit(db, mode, handoff, watermark, covered, tail_count=len(tail))
    _assert_unique(db, sid, [m["content"] for m in handoff])
    # Existing payloads, sidecars and ids remain recoverable, not rewritten/deleted.
    after = {row["id"]: row for row in db.get_messages("parent", include_inactive=True)}
    if mode != "rotation":
        assert (after[watermark]["active"], after[watermark]["compacted"]) == (0, 1)
    for original in before:
        assert {k: v for k, v in after[original["id"]].items() if k not in {"active", "compacted", "display_order"}} == {
            k: v for k, v in original.items() if k not in {"active", "compacted", "display_order"}}


@pytest.mark.parametrize("mode", ["watermark", "coverage", "rotation"])
def test_repeated_tail_generations_keep_newest_uid_once(db, mode):
    db.append_message("parent", role="user", content="summarized")
    watermark = db.get_active_message_watermark("parent")
    tail = _tail()
    db.append_messages_batch("parent", tail)
    # Reproduce historical active clone generations without the repair/upsert writer.
    newer = deepcopy(tail)
    newer[-1]["content"] = "newest reply"
    _duplicate_active_tail(db, tail)
    sid = _commit(db, mode, _summary(), watermark, [watermark])
    live = _assert_unique(db, sid, [_summary()[0]["content"], *[m["content"] for m in newer]])
    assert live[-2]["api_content"] == "verbatim result"
    if mode != "rotation":
        # A later compaction must not multiply the tail again.
        next_watermark = db.get_active_message_watermark(sid)
        db.archive_and_compact(sid, _summary() + deepcopy(newer), watermark=next_watermark)
        _assert_unique(db, sid, [_summary()[0]["content"], *[m["content"] for m in newer]])


@pytest.mark.parametrize("mode", ["watermark", "coverage", "rotation"])
def test_duplicate_uids_inside_handoff_keep_newest_position(db, mode):
    db.append_message("parent", role="user", content="summarized")
    watermark = db.get_active_message_watermark("parent")
    tail = _tail()
    handoff = _summary() + deepcopy(tail) + deepcopy(tail)
    sid = _commit(db, mode, handoff, watermark, [watermark])
    _assert_unique(db, sid, [_summary()[0]["content"], *[m["content"] for m in tail]])
    assert len(handoff) == 1 + len(tail)  # The immediate in-memory adoption is canonical too.


def test_replay_dedupes_active_uids_without_changing_archive(db):
    tail = _tail()
    db.append_messages_batch("parent", tail)
    newer = deepcopy(tail)
    newer[-1]["content"] = "newest reply"
    _duplicate_active_tail(db, tail)
    before = db.get_messages("parent", include_inactive=True)
    for include_row_ids in (False, True):
        replay = db.get_messages_as_conversation(
            "parent", repair_alternation=True, include_row_ids=include_row_ids)
        assert [msg["content"] for msg in replay] == [m["content"] for m in newer]
        assert len({msg["message_uid"] for msg in replay}) == len(replay)
    model, _display = db.get_resume_conversations("parent")
    assert [msg["content"] for msg in model] == [m["content"] for m in newer]
    assert model[-1]["_row_id"] == before[-1]["id"]
    assert db.get_messages("parent", include_inactive=True) == before
    # Explicit forensic reads still expose all stored generations.
    assert len(db.get_messages_as_conversation("parent", include_inactive=True)) == len(before)


def test_equal_content_with_distinct_uids_is_not_deduped(db):
    rows = [{"role": role, "content": "same", "message_uid": uid, "platform_message_id": "shared-platform-id"}
            for role, uid in (("user", "a"), ("assistant", "b"))]
    db.append_messages_batch("parent", rows)
    db.archive_and_compact("parent", deepcopy(rows))
    assert [m["message_uid"] for m in db.get_messages_as_conversation("parent", repair_alternation=True)] == ["a", "b"]


def test_handoff_shared_unidentified_dict_gets_one_uid(db):
    message = {"role": "assistant", "content": "same object"}
    db.archive_and_compact("parent", [message, message])
    assert len(db.get_messages("parent")) == 1


def test_replay_keeps_uidless_legacy_rows(db):
    db.append_messages_batch("parent", [{"role": role, "content": "same"} for role in ("user", "assistant")])
    db._write_sql("UPDATE messages SET message_uid = NULL WHERE session_id = ?", ("parent",))
    assert len(db.get_messages_as_conversation("parent", repair_alternation=True)) == 2


@pytest.mark.parametrize("rotate", [False, True])
def test_worker_handoff_duplicate_uids_adopt_one_dict_per_uid(tmp_path, rotate):
    """The worker facade adopts the owner's committed generation: a dropped duplicate dict would carry
    no row id back and be re-appended as a second active copy on the next flush."""
    from agent.runtime_session_store import RuntimeSessionStore
    from hermes_state_runtime import mutate_worker_execution, register_worker_execution
    from tests.fakes.runtime_owner import begin_runtime_epoch

    store_db = SessionDB(tmp_path / "worker.db")
    store_db.create_session("root", "cli", system_prompt="PREFIX")
    epoch = begin_runtime_epoch(store_db, instance_id="owner")
    scope = dict(epoch=epoch, execution_id="worker", session_id="root", generation=0)
    register_worker_execution(store_db, **scope, kind="compute", adoption_secret="secret")
    store = RuntimeSessionStore(lambda method, **params: mutate_worker_execution(store_db, **params),
                                scope, tmp_path / "outbox")
    try:
        tail = _tail()
        handoff = _summary() + deepcopy(tail) + deepcopy(tail)
        assert store.try_acquire_compression_lock("root", "holder")
        if rotate:
            store.publish_compression_child(parent_session_id="root", child_session_id="child", source="cli",
                messages=handoff, system_prompt="PREFIX", compression_lock_holder="holder")
        else:
            store.archive_and_compact("root", handoff, lock_holder="holder")
        sid = store.scope["session_id"]
        live = store_db.get_messages(sid)
        assert [m["message_uid"] for m in handoff] == [row["message_uid"] for row in live]
        assert [m["_row_id"] for m in handoff] == [row["id"] for row in live]
    finally:
        store.failure = None
        store.journal["pending"] = []
        store.close()
        store_db.close()
