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


def test_gateway_scale_receipts_track_live_anchors_and_return_to_baseline():
    from agent.source_origin import _origin_receipts

    gc.collect()
    baseline = len(_origin_receipts)
    anchors = [origin_metadata(NativeOrigin.person(
        f"Fictional message {index}", platform="telegram", principal="fictional", event=str(index)))
        for index in range(10_000)]
    assert len(_origin_receipts) == baseline + len(anchors)
    # Repeated shallow handoffs resolve the same anchor without growing the registry.
    for metadata in anchors:
        for _ in range(3):
            assert native_origin(dict(metadata)) is native_origin(metadata)
    del metadata
    assert len(_origin_receipts) == baseline + len(anchors)
    del anchors[:5_000]
    gc.collect()
    assert len(_origin_receipts) == baseline + len(anchors)
    anchors.clear()
    gc.collect()
    assert len(_origin_receipts) == baseline
