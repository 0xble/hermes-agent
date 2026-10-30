"""One conversation served on two routes (a moved Telegram topic and its Slack thread)."""
import asyncio
import dataclasses

import pytest

from gateway.platforms.event import MessageEvent
from tests.gateway.test_synthetic_native_admission import native_chat as native_chat  # noqa: F401
from tests.waiting import async_wait_until


def _second_route(runner, source):
    """Bind another route to the seeded session beside its first one, as a rebind does."""
    store = runner.session_store
    sid = store.lookup_by_session_key(runner._session_key_for_source(source)).session_id
    other = dataclasses.replace(source, chat_id="99", chat_type="group", thread_id="7001", message_id=None)
    key = runner._session_key_for_source(other)
    store.get_or_create_session(other, touch_activity=False)
    assert store.switch_session(key, sid, preserve_prompt_pin=False) is not None
    assert store.lookup_by_session_key(runner._session_key_for_source(source)).session_id == sid
    return sid, other


def _open(authority, sid):
    from hermes_state_runtime import list_session_admissions
    return [r for r in list_session_admissions(authority.db, session_id=sid) if r['status'] != 'terminal']


async def _say(adapter, source, text, message_id):
    await adapter.handle_message(MessageEvent(text=text, source=source, message_id=message_id))
    await async_wait_until(lambda: not adapter._background_tasks, timeout=20, what=text)


@pytest.mark.asyncio
async def test_both_routes_run_in_order_without_pausing(native_chat):  # noqa: F811
    runner, adapter, authority, source, sent, turns = native_chat
    sid, other = _second_route(runner, source)
    await _say(adapter, other, "from the second route", "m-2")
    await _say(adapter, source, "back on the first route", "m-3")
    await _say(adapter, other, "second route again", "m-4")
    await async_wait_until(lambda: len(turns) == 4 and not _open(authority, sid), timeout=20,
                           what="both routes drained")
    assert [(t.source.chat_id, t.text) for t in turns[1:]] == [
        ("99", "from the second route"), ("42", "back on the first route"), ("99", "second route again")]
    assert authority.sessions[sid].claim_route is None


@pytest.mark.asyncio
async def test_a_route_moved_to_another_session_is_still_refused(native_chat):  # noqa: F811
    from gateway.session_authority import LiveSession
    runner, adapter, authority, source, sent, turns = native_chat
    sid, other = _second_route(runner, source)
    live = authority.sessions.setdefault(sid, LiveSession(source, runner._session_key_for_source(source)))
    key = runner._session_key_for_source(other)
    runner.session_store.switch_session(key, "someone-else", preserve_prompt_pin=False)
    bound, _ = authority._route_binding(sid, live, key)
    assert bound is live.source  # not this owner's route: checked against the live route, which refuses it


@pytest.mark.asyncio
async def test_restart_recovers_rows_queued_on_both_routes(native_chat, monkeypatch):  # noqa: F811
    runner, adapter, authority, source, sent, turns = native_chat
    sid, other = _second_route(runner, source)
    held = []
    monkeypatch.setattr(authority, "_schedule", lambda ref: held.append(ref))
    for src, text, mid in ((other, "queued on route 2", "q-2"), (source, "queued on route 1", "q-1")):
        await adapter.handle_message(MessageEvent(text=text, source=src, message_id=mid))
    await async_wait_until(lambda: len(_open(authority, sid)) == 2, timeout=20, what="2 queued rows")
    for task in list(adapter._background_tasks):
        task.cancel()
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
    monkeypatch.undo()
    authority.sessions.pop(sid)  # a fresh process: nothing live yet
    store = runner.session_store
    bindings = [(authority.logical_owner(e.session_id), e.origin, runner._adapter_for_source(e.origin))
                for e in store.list_sessions() if e.origin is not None and e.session_id == sid]
    assert len(bindings) == 2
    assert await authority.recover_native_sessions(bindings) == {sid: "ready"}
    await async_wait_until(lambda: not _open(authority, sid), timeout=20, what="recovered rows drained")
    assert [t.text for t in turns[1:]] == ["queued on route 2", "queued on route 1"]


@pytest.mark.asyncio
async def test_recovery_still_refuses_ambiguous_bindings(native_chat):  # noqa: F811
    from hermes_state_runtime import RuntimeStoreError
    runner, adapter, authority, source, sent, turns = native_chat
    sid, other = _second_route(runner, source)
    with pytest.raises(RuntimeStoreError, match="admission_conflict"):
        authority._recovery_routes(sid, [(source, adapter), (source, adapter)])  # one route twice
    runner.session_store.switch_session(runner._session_key_for_source(other), "someone-else",
                                        preserve_prompt_pin=False)
    with pytest.raises(RuntimeStoreError, match="admission_conflict"):
        authority._recovery_routes(sid, [(source, adapter), (other, adapter)])  # route now elsewhere


def test_turn_keyed_state_follows_the_claimed_route(monkeypatch):
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    looked = []
    authority = SessionAuthority.__new__(SessionAuthority)
    authority.runner = type("R", (), {"_resident_agent_for": staticmethod(lambda route: looked.append(route))})()
    live = LiveSession(object(), "route:slack")
    authority.sessions = {"sid": live}
    ref = SessionRef("default", "sid")
    authority.agent(ref)
    live.claim_route = "route:telegram"
    authority.agent(ref)
    assert looked == ["route:slack", "route:telegram"]
    assert SessionAuthority._row_route(live, {"payload": {"native_text_v1": {"route": "route:telegram"}}}) \
        == "route:telegram"
    assert SessionAuthority._row_route(live, {"payload": {"local_operator_v1": {}}}) == "route:slack"
