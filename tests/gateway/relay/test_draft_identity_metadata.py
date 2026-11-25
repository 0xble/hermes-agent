"""Native draft identity stays gateway-local across both relay egress doors."""

import pytest

from tests.gateway.relay.test_relay_live_cards import _connected_adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["send", "send_for_platform", "explicit_send"])
@pytest.mark.parametrize("stream_open", [False, True])
async def test_draft_identity_does_not_reach_connector(lane, stream_open):
    adapter, stub = _connected_adapter()
    stub._identities = [("slack", "U-bot")]
    metadata = {"thread_id": "111.000", "_draft_id": 7, "notify": True}
    if stream_open:
        await adapter.send_draft("C1", 7, "partial", metadata={"thread_id": "111.000"})
    if lane == "send_for_platform":
        result = await adapter.send_for_platform("slack", "C1", "partial, done", metadata=metadata)
    else:
        if lane == "explicit_send":
            metadata["_relay_logical_platform"] = "slack"
        result = await adapter.send("C1", "partial, done", metadata=metadata)

    assert result.success
    assert stub.sent[-1]["op"] == ("draft" if stream_open else "send")
    assert "_draft_id" not in stub.sent[-1]["metadata"]
    assert stub.sent[-1]["metadata"]["thread_id"] == "111.000"
    assert metadata["_draft_id"] == 7
