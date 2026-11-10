"""The real shared-gateway CLI submission publishes only its live typed receipt privately."""
import json
from types import SimpleNamespace

import pytest

from tests.hermes_cli.test_gateway_agent import SID, frame, make_agent as scripted_agent


@pytest.fixture
def make_agent(request):
    return request.getfixturevalue("scripted_agent")


@pytest.mark.parametrize("typed", [True, False])
def test_shared_cli_origin_is_private_and_only_typed_input_can_publish(make_agent, tmp_path, monkeypatch, typed):
    from agent.source_origin import ORIGIN_KEY, typed_input_message
    from agent.source_origin_ingress import acquire_input, admission_input
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    text = "Keep notices off."
    agent, client = make_agent([frame("message.complete", {"text": "Acknowledged.", "outcome": "completed"})],
                               result={"final_response": "Acknowledged.", "completed": True})
    message = typed_input_message(text, "cli")
    agent._pending_cli_user_message = message if typed else json.loads(json.dumps(message))
    received = []
    original = client.rpc

    async def rpc(method, **params):
        if method == "prompt.submit":
            request = SimpleNamespace(ref=SimpleNamespace(session_id=SID), request_id=params["submission_id"])
            connection = SimpleNamespace(native_owner=True, authority=SimpleNamespace(
                db=SimpleNamespace(db_path=tmp_path / "fixture.db")))
            with acquire_input(connection, request.ref, params):
                received.append(admission_input(request))
            assert ORIGIN_KEY not in json.dumps(params) and "source_origin_v1" not in params
        return await original(method, **params)

    client.rpc = rpc
    result = agent.run_conversation(text)
    assert result["final_response"] == "Acknowledged."
    assert bool(received[0]) is typed
    assert list((tmp_path / "cache" / "native-input").glob("*.json")) == []
