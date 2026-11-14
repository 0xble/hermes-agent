"""A resume's display read (``get_resume_conversations``) is one read generation: a compaction committing between
its display SELECT and its removed-identities SELECT hides nothing (#150102 review; session.transcript reads
inside ``transcript_read_snapshot``, so its own race test no longer reaches ``_display_set``'s transaction).
"""
import pytest

from agent.context_compressor import SUMMARY_PREFIX
from hermes_state import SessionDB


@pytest.mark.parametrize("projection", ["resume", "conversation", "lineage_page"])
def test_a_compaction_between_a_resume_reads_two_selects_drops_nothing(tmp_path, monkeypatch, projection):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", source="cli")
        for i in (1, 2):
            db.append_message("s", "user", f"question {i}")
            db.append_message("s", "assistant", f"answer {i}")
        if projection == "lineage_page":
            db.create_session("parent", source="cli")
            monkeypatch.setattr(db, "_resume_lineage_ids", lambda _sid: ["parent", "s"])
        original, fired = db._with_removed_identities, []

        def compaction_lands_between(rows, session_ids, *rest, resident_only=False):
            if not fired:
                fired.append(True)
                tail = db.get_messages_as_conversation("s")[-2:]
                db.archive_and_compact("s", [{"role": "user", "content": f"{SUMMARY_PREFIX}\n## Goal\nx"}, *tail],
                                       watermark=db.get_active_message_watermark("s"), tail_count=2)
            return original(rows, session_ids, *rest, resident_only=resident_only)
        monkeypatch.setattr(db, "_with_removed_identities", compaction_lands_between)
        if projection == "resume":
            _, display = db.get_resume_conversations("s")
        elif projection == "conversation":
            display = db.get_messages_as_conversation("s", include_compacted=True)
        else:
            display = db.get_messages("s", include_ancestors=True, include_compacted=True)
        assert fired
        texts = [message.get("content") for message in display]
        assert [text for text in texts if text in {"question 2", "answer 2"}] == ["question 2", "answer 2"], texts
    finally:
        db.close()
