"""Shared CLI ingress uses a private occurrence record, never client JSON evidence."""
import copy
import json
from types import SimpleNamespace

import pytest

from agent.source_origin import typed_input_message, retention_user_text
from agent.source_origin_ingress import publish_input, acquire_input, admission_input, restore_input


@pytest.mark.parametrize("projection", ["native", "json", "deepcopy", "claimed"])
@pytest.mark.parametrize("native_owner", [True, False])
def test_only_native_typed_ingress_can_publish_and_rederive_an_occurrence(tmp_path, projection, native_owner):
    text = "Keep notices off."
    message = typed_input_message(text, "cli")
    if projection == "json":
        message = json.loads(json.dumps(message))
    elif projection == "deepcopy":
        message = copy.deepcopy(message)
    elif projection == "claimed":
        message["display_metadata"] = {"_hermes_source_origin": {"status": "verified_human"}}
    request = SimpleNamespace(ref=SimpleNamespace(session_id="fixture"), request_id="request")
    connection = SimpleNamespace(native_owner=native_owner, authority=SimpleNamespace(
        db=SimpleNamespace(db_path=tmp_path / "fixture.db")))
    params = {"session_id": "fixture", "submission_id": "request", "text": text}
    with publish_input(tmp_path, "fixture", "request", message, text):
        with acquire_input(connection, request.ref, params):
            record = admission_input(request)
        receipt = restore_input(record.get("source_origin_v1"), text, "admission")
        assert (receipt is not None) == (projection == "native" and native_owner)
        if receipt:
            assert receipt.spans == ((0, len(text)),) and receipt.platform == "cli"
            assert receipt.event == "admission"
            assert restore_input(record.get("source_origin_v1"), text, None) is None
        assert "origin" not in json.dumps(params)
        for changed in ({**params, "text": "Generated lookalike"}, {**params, "submission_id": "other"}):
            with acquire_input(connection, request.ref, changed):
                assert admission_input(request) == {}
    assert list((tmp_path / "cache" / "native-input").glob("*.json")) == []


def test_retry_reuses_only_the_authoritative_admission_origin():
    from agent.source_origin import native_origin
    from agent.source_origin_worker import pack_origin
    text = "Keep notices off."
    message = typed_input_message(text, "cli")
    packed = pack_origin(native_origin(message["display_metadata"]))
    request = SimpleNamespace(ref=SimpleNamespace(session_id="fixture"), request_id="request", payload={"text": text})
    committed = {"payload": {"text": text, "source_origin_v1": packed}}
    assert admission_input(request, committed) == {"source_origin_v1": packed}
    assert admission_input(request, {"payload": {"text": "different", "source_origin_v1": packed}}) == {}
    assert admission_input(request) == {}


def test_managed_child_receives_only_private_ingress_origin(monkeypatch):
    from contextlib import nullcontext
    from gateway import session_kanban
    from agent.source_origin import native_origin
    from agent.source_origin_worker import pack_origin
    from agent.turn_context import _stage_turn_user_message
    text = "Keep notices off."
    message = typed_input_message(text, "cli")
    frame = {"text": text, "source_origin_v1": pack_origin(native_origin(message["display_metadata"])),
             "input_event": "admission", "policy": {"kanban_json": "null"}}
    observed = []

    class Agent:
        def run_conversation(self, text, conversation_history, **kwargs):
            row, _ = _stage_turn_user_message(SimpleNamespace(platform="cli"), text, None, None,
                kwargs.get("persist_user_platform_id"), None, kwargs.get("persist_user_display_metadata"))
            observed.append(retention_user_text(text, [row]))
            return {"messages": [row]}

    monkeypatch.setattr(session_kanban, "_worker_workspace", lambda frame: nullcontext(None))
    session_kanban._run_worker_turns(Agent(), frame, [])
    session_kanban._run_worker_turns(Agent(), {**frame, "source_origin_v1": None}, [])
    assert observed == [text, None]


def test_imported_ink_pending_journal_cannot_mint_an_origin(tmp_path):
    request_id = "12345678-1234-1234-1234-123456789abc"
    request = SimpleNamespace(ref=SimpleNamespace(session_id="fixture"), request_id=request_id)
    connection = SimpleNamespace(native_owner=True, actor=SimpleNamespace(subject="fictional-person"),
        authority=SimpleNamespace(db=SimpleNamespace(db_path=tmp_path / "fictional.db")))
    text = "Keep notices off."
    params = {"submission_id": request_id, "text": text}
    record = {"submissionId": request_id, "text": text, "preparedText": text, "attempted": True,
              "ownerDestination": {"storedSid": "fixture", "profileHome": str(tmp_path)}}
    path = tmp_path / "tui-pending-inputs" / (request_id + ".json")
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(record))
    with acquire_input(connection, request.ref, {**params, "display_metadata": record}):
        assert admission_input(request) == {}
