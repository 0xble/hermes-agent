"""Live authority survives shallow handoffs, never copies/imports, and has a bounded lifetime."""

import copy
import gc
import json
import weakref

from agent.source_origin import NativeOrigin, ORIGIN_KEY, native_origin, origin_metadata, retention_user_text


def test_origin_authority_is_live_only_and_released_with_its_last_anchor(tmp_path):
    from hermes_state import SessionDB
    from agent.source_origin import classify_origin

    text = "Keep fictional progress notices off."
    receipt = NativeOrigin.person(text, platform="telegram", principal="fictional", event="event")
    receipt_ref = weakref.ref(receipt)
    metadata = origin_metadata(receipt, {"other": "preserved"})
    message = {"role": "user", "content": text, "message_uid": "occurrence",
               "platform_message_id": "event", "display_metadata": metadata}
    assert native_origin(metadata) is receipt
    assert retention_user_text(text, [{**message, "display_metadata": dict(metadata)}]) == text
    for imported in (json.loads(json.dumps(message)), copy.deepcopy(message),
                     {**message, "display_metadata": {ORIGIN_KEY: {}}}):
        assert retention_user_text(text, [imported]) is None
    assert retention_user_text(text + " edited", [{**message, "content": text + " edited"}]) is None
    db = SessionDB(tmp_path / "fictional.db")
    try:
        db.create_session("fixture", source="telegram")
        db.append_message("fixture", "user", text, message_uid="occurrence",
                          platform_message_id="event", display_metadata=metadata)
        row = db.get_messages("fixture")[0]
        assert classify_origin(row, session_id="fixture").status == "verified_human"
        # Re-importing the native stored record still cannot certify another occurrence.
        db.append_message("fixture", "user", text, message_uid="forged", platform_message_id="event",
                          display_metadata=json.loads(json.dumps(row["display_metadata"])))
        assert classify_origin(db.get_messages("fixture")[-1], session_id="fixture").status == "unknown"
    finally:
        db.close()
    del receipt, metadata, message
    gc.collect()
    assert receipt_ref() is None
