"""A producer wake is not refused over a forum topic name its committed envelope lacks."""
from dataclasses import replace

import pytest

from tests.gateway.test_synthetic_native_admission import native_chat as _native_chat_fixture
native_chat = _native_chat_fixture


def _automation(runner, source, text):
    from gateway.platforms.event import MessageEvent
    entry = runner.session_store.lookup_by_session_key(runner._session_key_for_source(source))
    event = MessageEvent(text=text, source=source, internal=True)
    event.metadata = {"gateway_session_key": entry.session_key, "gateway_session_id": entry.session_id}
    return event


@pytest.mark.asyncio
async def test_completion_admits_when_only_the_topic_name_differs(native_chat):
    """Telegram omits a forum topic's name on in-topic messages; the origin may have recorded it."""
    runner, adapter, authority, source, _sent, _turns = native_chat
    receipt = await authority.admit_automation(
        adapter, _automation(runner, replace(source, chat_topic="Have we setup cr"), "delegation done"),
        "deleg-1")
    assert receipt.status in {"queued", "started", "terminal"}


@pytest.mark.asyncio
async def test_completion_for_another_sender_is_still_refused(native_chat):
    from hermes_state_runtime import RuntimeStoreError
    runner, adapter, authority, source, _sent, _turns = native_chat
    event = _automation(runner, source, "delegation done")
    event.source = replace(source, user_id="43")
    with pytest.raises(RuntimeStoreError, match="admission_conflict"):
        await authority.admit_automation(adapter, event, "deleg-2")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"chat_id": "99"}, {"thread_id": "7001"}])
async def test_completion_for_another_chat_or_thread_is_still_refused(native_chat, change):
    from hermes_state_runtime import RuntimeStoreError
    runner, adapter, authority, source, _sent, _turns = native_chat
    event = _automation(runner, source, "delegation done")
    event.source = replace(source, chat_topic="Have we setup cr", **change)
    with pytest.raises(RuntimeStoreError, match="admission_conflict"):
        await authority.admit_automation(adapter, event, "deleg-3")
