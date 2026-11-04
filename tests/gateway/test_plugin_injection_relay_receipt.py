"""Optional cross-repository proof using the real session-relay consumer.

Expose agents/sources/plugins/session-relay on PYTHONPATH to execute; a normal
Hermes checkout need not have the external plugin's source/test harness installed.
"""
import asyncio

import pytest

relay_tests = pytest.importorskip("test_session_relay", reason="external session-relay checkout not supplied")
plugin, db, h = relay_tests.plugin, relay_tests.db, relay_tests.h

from hermes_state_runtime import ADMISSION_QUEUE_MAX, list_session_admissions
from tests.gateway.test_synthetic_native_admission import native_chat as native_chat  # noqa: E402, F401


@pytest.mark.asyncio
async def test_real_relay_receipt_queue_full_same_key_then_exactly_one_admission(native_chat, h, monkeypatch):
    runner, adapter, authority, source, sent, turns = native_chat
    runner._running = True
    runner._gateway_loop = asyncio.get_running_loop()
    monkeypatch.setattr(authority, "_schedule", lambda ref: None)
    route = runner._session_key_for_source(source)
    sid = runner.session_store.lookup_by_session_key(route).session_id
    for n in range(ADMISSION_QUEUE_MAX):
        result = await runner._schedule_plugin_message_injection(
            session_key=route, content=f"filler-{n}", plugin_id="session-relay", await_dispatch=True)
        assert result.accepted

    def inject(content, _key):
        return runner._schedule_plugin_message_injection(
            session_key=route, content=content, plugin_id="session-relay", await_dispatch=True,
            correlation_id="n1").result(timeout=10)

    h.deliverer.inject = inject
    h.ingest_on_inject = False
    await asyncio.to_thread(h.send, id="n1", fyi=True)
    receipt = h.mailbox.receipt("n1")  # persisted exact receipt, not sender's returned summary
    assert (receipt["status"], receipt["reason"], receipt["retryable"]) == ("refused", "queue_full", True)
    assert receipt["correlation_id"] == "n1" and h.mailbox.seen("n1") is None
    rows = list_session_admissions(authority.db, session_id=sid)
    assert len(rows) == ADMISSION_QUEUE_MAX
    assert not any("inter-session note id=n1" in str(row["payload"]) for row in rows)
    authority.db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='terminal' WHERE admission_id=?", (rows[0]["admission_id"],)))
    await asyncio.to_thread(h.send, id="n1", fyi=True)
    await asyncio.to_thread(h.send, id="n1", fyi=True)
    receipt = h.mailbox.receipt("n1")
    assert receipt["status"] == "pending" and receipt["admitted"] is True
    assert receipt["admission_id"]
    rows = list_session_admissions(authority.db, session_id=sid)
    admitted = [row for row in rows if "inter-session note id=n1" in str(row["payload"])]
    assert len(admitted) == 1 and admitted[0]["admission_id"] == receipt["admission_id"]
    assert len(turns) == 1
