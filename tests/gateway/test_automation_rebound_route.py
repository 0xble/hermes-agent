"""A session rebound to another route after its last human turn keeps receiving producer wakes."""
import dataclasses
import json

import pytest

from tests.gateway.test_synthetic_native_admission import native_chat as native_chat  # noqa: F401


def _rebind(runner, source):
    """Move the seeded session to another route, as the Slack cutover does (switch + unbind)."""
    store = runner.session_store
    old_key = runner._session_key_for_source(source)
    sid = store.lookup_by_session_key(old_key).session_id
    moved = dataclasses.replace(source, chat_id="99", chat_type="group", thread_id="7001", message_id=None)
    key = runner._session_key_for_source(moved)
    store.get_or_create_session(moved, touch_activity=False)
    assert store.switch_session(key, sid, preserve_prompt_pin=False) is not None
    assert store.unbind_session(old_key, sid)
    entry = store.lookup_by_session_key(key)
    assert entry.session_id == sid
    return key, entry, moved


def _last_automation_envelope(authority, key):
    with authority.db._read_ctx() as conn:
        row = conn.execute("SELECT payload_json FROM session_admissions WHERE principal_id=? "
                           "ORDER BY seq DESC LIMIT 1", ("automation:" + key,)).fetchone()
    return json.loads(row[0])["native_text_v1"]


def _wake(runner, entry, source, text="delegation done"):
    from gateway.platforms.event import MessageEvent
    event = MessageEvent(text=text, source=source, internal=True)
    event.metadata = {"gateway_session_key": entry.session_key, "gateway_session_id": entry.session_id}
    return event


@pytest.mark.asyncio
async def test_loop_wake_after_rebind_rides_the_current_route(native_chat):  # noqa: F811
    from gateway.session_automation import admit_route_wake, committed_route_envelope, route_rebound
    runner, adapter, authority, source, sent, turns = native_chat
    key, entry, moved = _rebind(runner, source)
    stale = committed_route_envelope(authority, entry)
    assert stale["source"]["chat_id"] == source.chat_id and route_rebound(entry, stale)

    assert await admit_route_wake(runner, adapter, runner._restored_source(entry), "tick", "loop-tick")

    envelope = _last_automation_envelope(authority, key)
    assert envelope["route"] == key
    assert (envelope["source"]["chat_id"], envelope["source"]["thread_id"]) == ("99", "7001")


@pytest.mark.asyncio
async def test_completion_after_rebind_is_admitted_on_the_current_route(native_chat):  # noqa: F811
    runner, adapter, authority, source, sent, turns = native_chat
    key, entry, moved = _rebind(runner, source)
    receipt = await authority.admit_automation(adapter, _wake(runner, entry, runner._restored_source(entry)),
                                               "deleg-rebound")
    assert receipt.status in {"queued", "started", "terminal"}
    envelope = _last_automation_envelope(authority, key)
    assert envelope["source"]["chat_id"] == "99" and envelope["route"] == key


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"user_id": "43"}, {"thread_id": "7002"}, {"chat_id": "100"}])
async def test_rebound_route_still_refuses_another_sender_or_route(native_chat, change):  # noqa: F811
    from hermes_state_runtime import RuntimeStoreError
    runner, adapter, authority, source, sent, turns = native_chat
    key, entry, moved = _rebind(runner, source)
    foreign = dataclasses.replace(runner._restored_source(entry), **change)
    with pytest.raises(RuntimeStoreError, match="admission_conflict"):
        await authority.admit_automation(adapter, _wake(runner, entry, foreign), "deleg-foreign")


@pytest.mark.asyncio
async def test_rebound_relay_origin_is_refused_without_borrowing_the_old_proof(native_chat, monkeypatch):  # noqa: F811
    """A rebound route revalidates its own origin; it never lends the old route's committed proof."""
    from hermes_state_runtime import RuntimeStoreError
    runner, adapter, authority, source, sent, turns = native_chat
    key, entry, moved = _rebind(runner, source)
    restored = runner._restored_source

    def relay_origin(entry_):
        relayed = restored(entry_)
        relayed.delivered_via_upstream_relay = True
        return relayed

    monkeypatch.setattr(runner, "_restored_source", relay_origin)
    with pytest.raises(RuntimeStoreError) as refused:
        await authority.admit_automation(adapter, _wake(runner, entry, restored(entry)), "deleg-relay")
    assert (refused.value.reason, refused.value.field) == ("not_found", "automation_legacy_delegation")


def test_unchanged_route_is_not_rebound():
    from types import SimpleNamespace
    from gateway.session_automation import route_rebound
    entry = SimpleNamespace(session_key="agent:main:discord:dm:42")
    assert not route_rebound(entry, {"route": "agent:main:discord:dm:42"})
    assert route_rebound(entry, {"route": "agent:main:telegram:dm:42:7"})
    assert not route_rebound(entry, None)
