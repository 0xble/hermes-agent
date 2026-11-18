"""Optional cross-repository proof using the real session-relay consumer.

Expose agents/sources/plugins/session-relay on PYTHONPATH to execute; a normal
Hermes checkout need not have the external plugin's source/test harness installed.
"""
import asyncio
import json
import re
from types import SimpleNamespace

import pytest

relay_tests = pytest.importorskip(
    "test_session_relay",
    reason="optional session-relay harness absent from PYTHONPATH; requires 0xble/agents#2014 "
           "(fix/relay-admission-refusal, 319717a7)",
)
plugin, db, h = relay_tests.plugin, relay_tests.db, relay_tests.h

from hermes_state_runtime import ADMISSION_QUEUE_MAX, list_session_admissions
from tests.gateway.test_synthetic_native_admission import native_chat


@pytest.mark.asyncio
async def test_real_relay_receipt_queue_full_same_key_then_exactly_one_admission(native_chat, h, monkeypatch):
    runner, _adapter, authority, source, _sent, turns = native_chat
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
        rid = re.match(r"\[inter-session note id=(\S+) ", content).group(1)
        return runner._schedule_plugin_message_injection(
            session_key=route, content=content, plugin_id="session-relay", await_dispatch=True,
            correlation_id=rid).result(timeout=10)

    # Drive the real keyed tool, mailbox and single drainer synchronously at its
    # wait boundary; only target discovery uses the plugin's disposable harness DB.
    def drain_and_read(mailbox, request, **kwargs):
        mailbox.enqueue(request)
        h.drainer.tick()
        h.drainer.tick()
        return mailbox.receipt(request["id"])

    monkeypatch.setattr(h.plugin, "send_and_wait", drain_and_read)
    tool = h.plugin.make_tool(SimpleNamespace(profile_name="default"), h.mailbox, None)
    args = {"target": "hermes:default/s-target", "text": "report", "fyi": True, "key": "factory-report"}
    h.deliverer.inject = inject
    first = json.loads(await asyncio.to_thread(tool, args, session_id="s-sender"))
    rid = first["id"]
    receipt = h.mailbox.receipt(rid)  # persisted exact receipt, not sender's summary
    missing = {"retryable", "correlation_id"} - receipt.keys()
    if missing:
        pytest.skip(
            "external session-relay lacks admission receipt fields "
            f"{sorted(missing)}; requires 0xble/agents#2014 (fix/relay-admission-refusal, 319717a7)"
        )
    assert (receipt["status"], receipt["reason"], receipt["retryable"]) == ("refused", "queue_full", True)
    assert receipt["correlation_id"] == rid and h.mailbox.seen(rid) is None
    rows = list_session_admissions(authority.db, session_id=sid)
    assert len(rows) == ADMISSION_QUEUE_MAX
    header = f"inter-session note id={rid}"
    assert not any(header in str(row["payload"]) for row in rows)
    authority.db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='terminal' WHERE admission_id=?", (rows[0]["admission_id"],)))
    second = json.loads(await asyncio.to_thread(tool, args, session_id="s-sender"))
    third = json.loads(await asyncio.to_thread(tool, args, session_id="s-sender"))
    assert first["id"] == second["id"] == third["id"]
    receipt = h.mailbox.receipt(rid)
    assert receipt["status"] == "pending" and receipt["admitted"] is True
    assert receipt["admission_id"]
    rows = list_session_admissions(authority.db, session_id=sid)
    admitted = [row for row in rows if header in str(row["payload"])]
    assert len(admitted) == 1 and admitted[0]["admission_id"] == receipt["admission_id"]
    assert len(turns) == 1
