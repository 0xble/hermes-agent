"""Native refusal evidence uses the existing runtime writer and never messages users."""
from collections import deque
from time import monotonic as real_monotonic
import json
import os

import pytest

from gateway import admission_canary, status
from tests.gateway.test_synthetic_native_admission import goal_prompt, native_chat as native_chat
from tests.hermes_cli.test_admission_canary import switched as switched


@pytest.fixture
def async_switched(switched, monkeypatch):
    # The synchronous guardian fixture freezes global monotonic time. Asyncio's
    # transport tasks need its real clock, while evidence keeps the fake wall time.
    monkeypatch.setattr("time.monotonic", real_monotonic)
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", switched[0] / "state.db")
    return switched


@pytest.fixture
def refusal_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(admission_canary, "_events", deque(maxlen=4096))
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: tmp_path / "gateway_state.json")
    monkeypatch.setattr(status, "_runtime_status_state_path", None)
    monkeypatch.setattr(status, "_runtime_status_state", None)
    monkeypatch.setattr(status, "_get_code_identity_fields", lambda: {"code_sha": "b" * 40})
    return tmp_path / "gateway_state.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["user", "goal", "automation"])
@pytest.mark.parametrize("closed", [False, True])
async def test_each_refused_admission_counts_once_before_notice_dedup(
        refusal_evidence, native_chat, producer, closed):
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress import admit_message
    from hermes_state_runtime import RuntimeStoreError

    runner, adapter, authority, source, sent, turns = native_chat
    assert admission_canary.snapshot()["refusals"] == []  # accepted seed does not count
    entry = runner.session_store.lookup_by_session_key(runner._session_key_for_source(source))
    event = (goal_prompt(runner, source, "goal wake") if producer == "goal"
             else MessageEvent(text="notice", source=source))
    event.media_urls = "invalid"
    if producer == "automation":
        event.internal = True
        event.metadata = {"gateway_session_key": entry.session_key,
                          "gateway_session_id": entry.session_id, "foreign": True}
    authority.retiring = closed
    before = list(sent)
    try:
        for index in range(5):
            if producer == "automation" or closed:
                # Distinct producer identities: each is its own refused admission.
                admit = (authority.admit_automation(adapter, event, f"notice-{index}") if producer == "automation"
                         else authority.admit_native(event))
                with pytest.raises(RuntimeStoreError) as refused:
                    await admit
                reason, field = refused.value.reason, refused.value.field
            else:
                reply = await admit_message(authority, event)
                assert bool(reply) is (producer == "user" and index == 0)
                reason, field = "invalid_params", "media_urls"
            evidence = admission_canary.snapshot()["refusals"]
            assert len(evidence) == index + 1
            lane = "automation" if producer == "automation" else "native"
            clause = f"{lane}: {reason}" + (f" ({field})" if field else "")
            assert evidence[-1][1] == clause
        assert sent == before and len(turns) == 1
    finally:
        authority.retiring = False


@pytest.mark.asyncio
async def test_retries_of_one_refused_producer_identity_count_once_per_window(
        refusal_evidence, native_chat, monkeypatch):
    """A requeued notice retried on backoff is one failed admission, not one per retry."""
    from hermes_state_runtime import RuntimeStoreError

    runner, adapter, authority, source, sent, turns = native_chat
    entry = runner.session_store.lookup_by_session_key(runner._session_key_for_source(source))
    clock = [1000.0]
    monkeypatch.setattr(admission_canary.time, "time", lambda: clock[0])

    async def refuse(identity):
        from gateway.platforms.event import MessageEvent
        event = MessageEvent(text="notice", source=source, internal=True)
        event.metadata = {"gateway_session_key": entry.session_key,
                          "gateway_session_id": entry.session_id, "foreign": True}
        with pytest.raises(RuntimeStoreError):
            await authority.admit_automation(adapter, event, identity)

    for _ in range(11):
        await refuse("stuck-completion")
        clock[0] += 15
    assert len(admission_canary.snapshot()["refusals"]) == 1
    await refuse("other-completion")
    assert len(admission_canary.snapshot()["refusals"]) == 2
    clock[0] += 601
    await refuse("stuck-completion")  # a new window counts the still-failing producer again
    assert [at for at, _ in admission_canary.snapshot()["refusals"]] == [clock[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("prepares", [False, True])
async def test_producer_preparation_and_nested_authority_refusal_count_once(
        refusal_evidence, native_chat, monkeypatch, prepares):
    from gateway.platforms.event import MessageEvent
    from gateway.platforms.webhook_ingress import admit_producer
    from hermes_state_runtime import RuntimeStoreError

    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _: prepares)
    runner, adapter, authority, source, sent, turns = native_chat
    before = list(sent)
    for index in range(5):
        event = MessageEvent(text="notice", source=source)
        event.media_urls = "invalid"
        with pytest.raises(RuntimeStoreError, match="invalid_params"):
            await admit_producer(adapter, event)
        assert len(admission_canary.snapshot()["refusals"]) == index + 1
    assert {clause for _, clause in admission_canary.snapshot()["refusals"]} == {
        "native: invalid_params (media_urls)"}
    assert sent == before and len(turns) == 1


@pytest.mark.asyncio
async def test_scheduled_admissions_do_not_inherit_parent_counting_scope(refusal_evidence, native_chat):
    import asyncio
    from gateway.platforms.event import MessageEvent
    from hermes_state_runtime import RuntimeStoreError

    _, _, authority, source, _, _ = native_chat
    event = MessageEvent(text="notice", source=source)
    event.media_urls = "invalid"
    with admission_canary.refusal_evidence("native"):
        for index in range(3):
            with pytest.raises(RuntimeStoreError, match="invalid_params"):
                await asyncio.create_task(authority.admit_native(event))
            assert len(admission_canary.snapshot()["refusals"]) == index + 1


@pytest.mark.asyncio
async def test_real_goal_wake_refusal_spike_fails_canary_and_rolls_back_once(
        async_switched, refusal_evidence, native_chat, monkeypatch):
    from gateway.wake import admit_internal_event
    from hermes_cli import admission_canary as canary
    from tests.waiting import async_wait_until

    home, _, _, old, candidate, clock, rollbacks = async_switched
    runner, adapter, authority, source, sent, turns = native_chat
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: home / "gateway_state.json")
    last = home / "release-last-txn.json"
    record = json.loads(last.read_text())
    record["reload_ack"]["gateway_pid"] = os.getpid()
    last.write_text(json.dumps(record))
    clock[0] = 1001
    before = list(sent)
    for index in range(10):
        event = goal_prompt(runner, source, f"goal wake {index}")
        event.media_urls = "invalid"
        await admit_internal_event(adapter, event)
        await async_wait_until(lambda: not adapter._background_tasks, timeout=20,
                               what="refused goal wake")
        assert status.flush_runtime_status()
        signal = json.loads((home / "gateway_state.json").read_text())["native_admission"]
        assert signal["pid"] == os.getpid() and len(signal["refusals"]) == index + 1
        outcome, _, detail = canary.inspect(home)
        assert outcome == ("watching" if index < 9 else "tripped")
    assert detail["refusals"] == 10
    assert detail["dominant_clause"] == "native: invalid_params (media_urls)"
    assert not rollbacks and sent == before and len(turns) == 1
    for _ in range(2):
        with pytest.raises(canary.AdmissionCanaryError, match="10 refusals"):
            canary.observe(home, candidate)
    assert rollbacks == [old] and (home / "current").resolve() == old
    failure = canary.read_state(home)
    assert failure["outcome"] == "failed" and failure["rollback_healthy"] is True
    assert failure["refusals"] == 10
    assert sent == before


def test_refusals_publish_timestamped_runtime_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(admission_canary, "_events", deque(maxlen=4096))
    monkeypatch.setattr(admission_canary.time, "time", lambda: 1000.25)
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: tmp_path / "gateway_state.json")
    monkeypatch.setattr(status, "_runtime_status_state_path", None)
    monkeypatch.setattr(status, "_runtime_status_state", None)
    monkeypatch.setattr(status, "_get_code_identity_fields", lambda: {"code_sha": "b" * 40})
    admission_canary.record_refusal("invalid_params (metadata)")
    admission_canary.record_refusal("invalid_params (text)")
    assert status.flush_runtime_status()
    payload = json.loads((tmp_path / "gateway_state.json").read_text())
    assert payload["native_admission"] == {
        "pid": os.getpid(), "refusals": [[1000.25, "invalid_params (metadata)"], [1000.25, "invalid_params (text)"]],
    }
    assert payload["code_sha"] == "b" * 40


def test_evidence_is_bounded_and_does_not_mask_refusal(monkeypatch):
    monkeypatch.setattr(admission_canary, "_events", deque([(0, "old", None), (999, "metadata", None)], maxlen=4096))
    monkeypatch.setattr(admission_canary.time, "time", lambda: 1000)
    def failed_publish():
        raise OSError("status disk unavailable")
    monkeypatch.setattr(status, "publish_runtime_status", failed_publish)
    admission_canary.record_refusal("text")
    assert admission_canary.snapshot()["refusals"] == [(999, "metadata"), (1000, "text")]
    for _ in range(4100):
        admission_canary.record_refusal("metadata")
    assert len(admission_canary.snapshot()["refusals"]) == 4096
