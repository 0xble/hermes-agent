"""Explicit session resets create empty context without destroying the original transcript.

Compression exhaustion now retains its session instead of invoking this reset. An explicit reset
must still select a clean, parentless session rather than leaking a compressed child's history
into the new conversation (#35809).
"""

from __future__ import annotations

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Behavioral contract: reset yields a clean next-turn transcript
# ---------------------------------------------------------------------------
def _make_store(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    # Isolate the SQLite transcript store so we exercise per-session_id
    # transcripts without touching the developer's real state.db.
    store._db = SessionDB(db_path=tmp_path / "state.db")
    return store


def _make_source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")


def _bloat(n):
    # Stand-in for the oversized, post-compression "child" transcript that
    # could not be compressed any further (#35809). Alternates roles so the
    # fixture is a valid conversation: load_transcript is a live-replay
    # restore site and heals alternation violations on load (#64934), so a
    # degenerate all-user transcript would be merged into one message.
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": "x" * 2000,
        }
        for i in range(n)
    ]


class TestExplicitResetLoadsCleanContext:
    """Explicit reset must load an empty transcript for the new session, not the old child."""

    def test_next_turn_transcript_is_empty_after_explicit_reset(self, tmp_path):
        store = _make_store(tmp_path)
        source = _make_source()

        entry = store.get_or_create_session(source)
        session_key = entry.session_key
        bloated_sid = entry.session_id
        store._db.create_session(
            session_id=bloated_sid, source="telegram", user_id="u1"
        )
        store._db.replace_messages(bloated_sid, _bloat(120))
        assert len(store.load_transcript(bloated_sid)) == 120  # precondition

        new_entry = store.reset_session(session_key)
        assert new_entry is not None
        assert new_entry.session_id != bloated_sid

        resolved = store.get_or_create_session(source)
        assert resolved.session_id == new_entry.session_id
        loaded = store.load_transcript(resolved.session_id)

        assert loaded == [], (
            f"Explicit reset must yield an empty context, got {len(loaded)} "
            f"messages — the bloated compressed child leaked into the new session."
        )
        # The old transcript is still searchable, not destroyed.
        assert len(store.load_transcript(bloated_sid)) == 120

