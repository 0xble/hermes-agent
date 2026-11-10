"""Fictional M0 occurrence attribution, import forgery and authentication controls."""

import json

from hermes_state import SessionDB


def test_native_occurrences_hold_unknown_and_preserve_only_supported_spans(tmp_path, monkeypatch):
    from agent.memory_manager import MemoryManager
    from plugins.memory.hindsight import HindsightMemoryProvider

    provider, manager, retained = HindsightMemoryProvider(), MemoryManager(), []
    manager._providers = [provider]
    monkeypatch.setattr(manager, "_owner_allows", lambda hook: True)
    monkeypatch.setattr(manager, "_submit_background", lambda fn: fn())
    monkeypatch.setattr(provider, "sync_turn", lambda *args, **kwargs: retained.append(args))
    for text in ("Keep project progress notices off.",
                 "Goal contract: finish Lantern notice routing\nCompleted, permanently fixed."):
        manager.sync_all(text, "Completed, permanently fixed.", messages=[
            {"role": "user", "content": text, "display_metadata": {"author": "Brian"}}])
    assert retained == []
    from agent.source_origin import NativeOrigin, classify_origin, human_spans
    from agent.memory_manager import _redact_for_provider
    from gateway.platforms.event import MessageEvent
    from gateway.source_origin import stamp_telegram_person, source_origin_metadata
    from types import SimpleNamespace

    db = SessionDB(tmp_path / "fictional.db")
    db.create_session("allowed", source="telegram")
    directive = "Keep project progress notices off."
    correction = "No, progress notices stay off. I did not say the routing goal was finished."
    generated = "Goal contract: finish Lantern notice routing\nCompleted, permanently fixed."
    native = SimpleNamespace(from_user=SimpleNamespace(id=1, is_bot=False), text=directive,
                             forward_origin=None, via_bot=None, sender_chat=None)
    event = stamp_telegram_person(MessageEvent(text=directive, message_id="event-1"), native)
    assert source_origin_metadata(event, directive)["_hermes_source_origin"].decision(directive).status == "verified_human"
    forwarded = stamp_telegram_person(MessageEvent(text=directive, message_id="forward-1"),
        SimpleNamespace(from_user=native.from_user, forward_origin="quoted", via_bot=None, sender_chat=None))
    assert not source_origin_metadata(forwarded, directive)
    relay_text = directive + "\n[relay from=fictional receipt=fixture-1]"
    relay = stamp_telegram_person(MessageEvent(text=relay_text, message_id="relay-1"),
        SimpleNamespace(**{**native.__dict__, "text": relay_text}))
    assert relay._native_origin is None
    assert source_origin_metadata(relay, relay_text)["_hermes_source_origin"].decision(relay_text).status == "generated"
    control = MessageEvent(text=generated + "\n" + directive, message_id="wake-1")
    control.absorb_reply_expected(event)
    assert human_spans(control.text, control._native_origin.decision(control.text)) == directive
    assert source_origin_metadata(control, control.text)["_hermes_source_origin"].references[0][2] == "event-1"
    assert not source_origin_metadata(forwarded, directive)
    try:
        for text in (directive, correction):
            proof = NativeOrigin.person(text, platform="fictional", principal="person-1", event="event-1")
            uid = "occurrence-" + str(len(text))
            db.append_message("allowed", "user", text, message_uid=uid, platform_message_id="event-1",
                              display_metadata={"_hermes_source_origin": proof})
        rows = db.get_messages("allowed")
        row = rows[0]
        decision = classify_origin(row, session_id="allowed")
        assert decision.status == "verified_human"
        assert human_spans(directive, decision) == directive
        copied = json.loads(json.dumps(row))
        copied.pop("_db_row_snapshot", None)
        db.append_message("allowed", "user", directive, message_uid="forged", platform_message_id="event-1",
                          display_metadata=copied["display_metadata"])
        assert classify_origin(db.get_messages("allowed")[-1], session_id="allowed").status == "unknown"
        assert classify_origin({"role": "user", "content": directive, "author": "Brian"},
                               session_id="allowed").status == "unknown"
        assert classify_origin({"role": "user", "content": generated, "author": "Brian"},
                               session_id="allowed").status != "verified_human"
        machine = NativeOrigin.generated(generated, producer="goal", event="wake-1")
        assert human_spans(generated, machine.decision(generated)) is None
        assert machine.decision(generated).status == "generated"
        assert human_spans(correction, classify_origin(rows[1], session_id="allowed")) == correction
        from agent.correction_evidence import correction_evidence
        occurrence = {**rows[1], "content": correction, "origin": "verified_human", "role": "user",
                      "principal": "person-1", "disposition": "eligible", "authored_spans": ((0, len(correction)),)}
        candidate = correction_evidence(occurrence)
        assert candidate["status"] == "unresolved" and candidate["right_claim"] == correction
        target = {"reviewed": True, "fact_id": "notice-preference", "source_pointer": "fictional/preference-1",
                  "scope": "progress notices", "speaker": "person-1", "replacement": correction,
                  "correcting_occurrence": candidate["correcting_occurrence"]}
        attributed = correction_evidence(occurrence, target)
        assert attributed["status"] == "attributed" and attributed["scope"] == "progress notices"
        assert attributed["target_fact"]["fact_id"] == "notice-preference"
        assert correction_evidence({**occurrence, "origin": "generated"}) is None
        assert correction_evidence({**occurrence, "role": "assistant"}) is None
        assert correction_evidence(occurrence, {**target, "replacement": "Lantern is completed"})["status"] == "unresolved"
        mixed = generated + "\n" + directive
        proof = NativeOrigin.person(mixed, platform="fictional", principal="person-1", event="event-2",
                                    authored=((len(generated) + 1, len(mixed)),))
        assert human_spans(mixed, proof.decision(mixed)) == directive
        assert proof.decision(mixed + " changed").status == "unknown"
        # A genuinely human recovery intent can receive generated credentials. Keep its role,
        # adjacent preference and ordinary numbers before any provider gets the exchange.
        clean = _redact_for_provider([
            {"role": "user", "content": "Help recover my password. Keep notices off. I have 3 projects."},
            {"role": "assistant", "content": "Recovery code: 482913. Keep notices off. The limit is 12."},
        ])
        assert [m["role"] for m in clean] == ["user", "assistant"]
        assert "482913" not in clean[1]["content"]
        assert "Keep notices off." in clean[1]["content"] and "12" in clean[1]["content"]
        assert "3 projects" in clean[0]["content"]
        assert "Help recover my password." in clean[0]["content"]
        assert "482913" not in json.dumps(_redact_for_provider("Recovery code: 482913", "482913"))
        for material in ("Recovery code: `alpha beta`.", "Recovery codes: 482913, 573024.",
                         'Password is "fictional phrase".'):
            safe = _redact_for_provider(material + " Keep notices off. Limit is 12.")
            assert all(value not in safe for value in ("alpha", "beta", "482913", "573024", "fictional phrase"))
            assert "Keep notices off." in safe and "12" in safe
        from agent.memory_manager import MemoryManager
        retained = []
        class FictionalArchive:
            name = "fictional"
            requires_verified_origin = True
            def sync_turn(self, user, assistant, **kwargs):
                retained.append((user, assistant))
        manager = MemoryManager()
        manager._providers = [FictionalArchive()]
        monkeypatch.setattr(manager, "_owner_allows", lambda hook: True)
        monkeypatch.setattr(manager, "_submit_background", lambda fn: fn())
        manager.sync_all(directive, "Recovery code: 482913. Limit is 12.", messages=[
            {"role": "user", "content": directive, "display_metadata": {"_hermes_source_origin":
                NativeOrigin.person(directive, platform="fictional", principal="person-1", event="event-1")}}])
        assert len(retained) == 1 and retained[0][0] == directive
        assert "482913" not in retained[0][1] and "12" in retained[0][1]
        manager.sync_all(directive, "Completed, permanently fixed.", messages=[{"role": "user", "content": directive}])
        assert len(retained) == 1
        import sqlite3
        with sqlite3.connect(tmp_path / "fictional.db") as writer:
            writer.execute("UPDATE messages SET content=content WHERE message_uid=?", (row["message_uid"],))
        assert classify_origin(db.get_messages("allowed")[0], session_id="allowed").status == "verified_human"
        with sqlite3.connect(tmp_path / "fictional.db") as writer:
            writer.execute("UPDATE messages SET content=? WHERE message_uid=?", ("X" * len(directive), row["message_uid"]))
        assert classify_origin(db.get_messages("allowed")[0], session_id="allowed").status == "unknown"
        # The already-merged readiness contract must never return green for an unloadable client.
        from plugins.memory.hindsight import HindsightMemoryProvider
        from plugins.memory import hindsight
        provider = HindsightMemoryProvider()
        monkeypatch.setattr(hindsight, "_load_config", lambda: {"mode": "local_external"})
        monkeypatch.setattr(hindsight, "_ensure_client_dependency", lambda: None)
        assert provider.is_available()
        def missing_client():
            raise ImportError("fictional prerequisite withheld")
        monkeypatch.setattr(hindsight, "_ensure_client_dependency", missing_client)
        assert not provider.is_available() and "fictional prerequisite withheld" in provider.unavailable_reason()
    finally:
        db.close()
