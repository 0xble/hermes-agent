"""Durable result replay preserves messages without serializing native origin authority."""

import pytest

from agent.source_origin import NativeOrigin, ORIGIN_KEY, retention_user_text
from gateway.session_results import admission_result, finish_result, prepare_result, retain_result
from hermes_state_runtime import admit_session_input, claim_session_input


@pytest.mark.parametrize("settlement", ["retain", "prepared"])
@pytest.mark.parametrize("origin", ["human", "generated"])
def test_durable_result_omits_native_receipt_and_preserves_live_messages(owner, settlement, origin):
    text = "Keep project progress notices off."
    receipt = (NativeOrigin.person(text, platform="telegram", principal="fictional", event="event")
               if origin == "human" else NativeOrigin.generated(text, producer="supervisor", event="event"))
    owner.db.create_session("fictional", source="local")
    admit_session_input(owner.db, epoch=owner.epoch, principal_id="api", session_id="fictional",
                        request_id="request", payload={"text": text})
    row = claim_session_input(owner.db, epoch=owner.epoch, session_id="fictional")
    message = {"role": "user", "content": text,
               "display_metadata": {ORIGIN_KEY: receipt, "other": "preserved"}}
    result = {"result": {"final_response": "The setting is saved.", "_messages_are_turn_suffix": True,
                         "messages": [message, {"role": "assistant", "content": "The setting is saved."}]},
              "usage": {"input_tokens": 3}}
    if settlement == "retain":
        retain_result(owner.db, epoch=owner.epoch, row=row, result=result)
    else:
        prepared = prepare_result(row, "The setting is saved.", "completed", result)
        finish_result(owner.db, epoch=owner.epoch, row=row, response="The setting is saved.",
                      outcome="completed", result=result, prepared=prepared)
    saved = admission_result(owner.db, row["admission_id"])
    assert saved["result"]["final_response"] == "The setting is saved."
    assert saved["usage"] == {"input_tokens": 3}
    assert saved["result"]["messages"][0] == {
        "role": "user", "content": text, "display_metadata": {"other": "preserved"}}
    assert message["display_metadata"][ORIGIN_KEY] is receipt
    assert result["result"]["messages"][0] is message
    assert retention_user_text(text, saved["result"]["messages"]) is None
