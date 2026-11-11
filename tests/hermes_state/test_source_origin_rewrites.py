"""Stored occurrence authority survives metadata rewrites, never edits or imports."""

import json

import pytest

from agent.source_origin import NativeOrigin, ORIGIN_KEY, classify_origin
from hermes_state import SessionDB


@pytest.mark.parametrize("rewrite", ["reaction", "seen", "display_kind", "replace", "batch", "forged",
                                      "changed_content", "changed_uid", "changed_event", "changed_role", "tampered_record"])
def test_origin_survives_only_unchanged_stored_occurrences(tmp_path, rewrite):
    db = SessionDB(tmp_path / "fictional.db")
    text = "Keep project notices off."
    try:
        db.create_session("fixture", source="telegram")
        db.append_message("fixture", "user", text, message_uid="occurrence", platform_message_id="event",
                          display_metadata={ORIGIN_KEY: NativeOrigin.person(
                              text, platform="fictional", principal="person", event="event")})
        row = db.get_messages("fixture")[0]
        assert classify_origin(row, session_id="fixture").status == "verified_human"
        if rewrite in {"reaction", "seen"}:
            db.set_message_reaction("fixture", row["id"], "+1", author="agent")
            if rewrite == "seen":
                assert db.take_unseen_reactions("fixture", author="agent")
        elif rewrite == "display_kind":
            forged = {**json.loads(json.dumps(row["display_metadata"].origin)), "principal": "forged"}
            assert db.set_latest_matching_message_display_kind(
                "fixture", role="user", content=text, display_kind="chat", display_metadata={ORIGIN_KEY: forged})
        else:
            messages = db.get_messages_as_conversation("fixture")
            if rewrite == "forged":
                messages = json.loads(json.dumps(messages))
            messages[0].update({
                "changed_content": {"content": "X" * len(text)},
                "changed_uid": {"message_uid": "different-occurrence"},
                "changed_event": {"message_id": "different-event"},
                "changed_role": {"role": "assistant"},
            }.get(rewrite, {}))
            if rewrite == "tampered_record":
                messages[0]["display_metadata"].origin["principal"] = "forged"
            if rewrite == "batch":
                db.archive_and_compact("fixture", messages)
            else:
                db.replace_messages("fixture", messages)
        actual = db.get_messages("fixture")[-1]
        decision = classify_origin(actual, session_id="fixture")
        expected = "verified_human" if rewrite in {"reaction", "seen", "display_kind", "replace", "batch"} else "unknown"
        assert decision.status == expected
        if expected == "verified_human":
            assert decision.principal == "person" and decision.event == "event"
    finally:
        db.close()
