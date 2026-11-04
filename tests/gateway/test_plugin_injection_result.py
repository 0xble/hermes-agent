"""Correlated admission receipts, following upstream #96179, against the real FIFO."""
import asyncio

import pytest

from hermes_state_runtime import ADMISSION_QUEUE_MAX, list_session_admissions
from tests.gateway.test_synthetic_native_admission import native_chat as native_chat  # noqa: F401


def inject(runner, key, text, correlation_id="relay-note"):
    return runner._schedule_plugin_message_injection(
        session_key=key, content=text, plugin_id="session-relay",
        await_dispatch=True, correlation_id=correlation_id,
    )


def setup(runner):
    runner._running = True
    runner._gateway_loop = asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_queue_refusal_is_retryable_but_admitted_delayed_work_is_not(native_chat, monkeypatch):
    runner, adapter, authority, source, sent, turns = native_chat
    setup(runner)
    key = runner._session_key_for_source(source)
    entry = runner.session_store.lookup_by_session_key(key)
    monkeypatch.setattr(authority, "_schedule", lambda ref: None)
    for n in range(ADMISSION_QUEUE_MAX):
        result = await inject(runner, key, f"filler-{n}", f"filler-{n}")
        assert result.accepted and result.admission_id
    refused = await inject(runner, key, "keyed relay note")
    assert refused.refused and refused.safe_to_retry
    assert refused.reason == "queue_full" and refused.correlation_id == "relay-note"
    rows = list_session_admissions(authority.db, session_id=entry.session_id)
    assert len(rows) == ADMISSION_QUEUE_MAX
    assert all("keyed relay note" not in str(row["payload"]) for row in rows)
    authority.db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='terminal' WHERE admission_id=?", (rows[0]["admission_id"],)))
    accepted = await inject(runner, key, "keyed relay note")
    assert accepted.accepted and accepted.reason == "adopted" and accepted.admission_id
    assert not accepted.refused and not accepted.safe_to_retry
    rows = list_session_admissions(authority.db, session_id=entry.session_id)
    assert sum("keyed relay note" in str(row["payload"]) for row in rows) == 1
    assert len(turns) == 1  # admission is acknowledged without waiting for execution


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout_after_commit", "cancel_after_commit", "draining"])
async def test_dispatch_observation_never_turns_possible_commit_into_refusal(native_chat, monkeypatch, mode):
    runner, adapter, authority, source, sent, turns = native_chat
    setup(runner)
    key = runner._session_key_for_source(source)
    monkeypatch.setattr(authority, "_schedule", lambda ref: None)
    if mode == "draining":
        runner._draining = True
        result = await inject(runner, key, "drained")
        assert result.refused and result.reason == "gateway_draining"
        return
    committed, release = asyncio.Event(), asyncio.Event()
    original = authority.admit_automation

    async def delayed(*args, **kwargs):
        receipt = await original(*args, **kwargs)
        committed.set()
        await release.wait()
        return receipt

    monkeypatch.setattr(authority, "admit_automation", delayed)
    handle = inject(runner, key, "late receipt")
    await asyncio.wait_for(committed.wait(), 10)
    observed = await asyncio.to_thread(handle.result, 0)
    assert observed.reason == "timeout" and observed.indeterminate and not observed.safe_to_retry
    if mode == "cancel_after_commit":
        handle.cancel()
        await asyncio.sleep(0)
        result = await handle
        assert result.indeterminate and not result.refused and not result.safe_to_retry
    else:
        release.set()
        result = await handle
        assert result.accepted and result.admission_id and result.correlation_id == "relay-note"
    entry = runner.session_store.lookup_by_session_key(key)
    assert len(list_session_admissions(authority.db, session_id=entry.session_id)) == 1
    release.set()
