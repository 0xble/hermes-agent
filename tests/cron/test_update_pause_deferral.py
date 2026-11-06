"""Only a typed, known pre-admission update fence is a deferral, never a run failure."""
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from cron import executions, incidents, jobs, scheduler, scheduler_authority
from hermes_cli import gateway_client, gateway_runtime
from hermes_time import now


@pytest.fixture
def refusal(monkeypatch):
    """Exercise the real discovery -> connect fence without any socket, agent or live gateway."""
    from gateway import session_cron
    monkeypatch.setattr(session_cron, "owner_for_home", lambda _home: None)
    monkeypatch.setattr(gateway_runtime, "ensure_gateway_runtime", lambda *a, **k:
                        gateway_runtime.GatewayDiscovery("draining", reason_code="update_paused"))
    dial = Mock(side_effect=AssertionError("update fence must precede connection"))
    monkeypatch.setattr(gateway_client, "gateway_ws_target", dial)
    return dial


def _run_body(job):
    # Run the real worker body, but never launch a detached worker or run an agent.
    return scheduler._run_one_job_body(job, adapters=None, loop=None, verbose=False,
                                       extra_prompt=None, claim_lost=None, transport_cancel=None)


def _claim(job):
    row = executions.create_execution(job["id"], source="test", scheduled_instant=job["next_run_at"])
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    claimed["_scheduled_instant"] = job["next_run_at"]
    claimed["execution_id"] = row["id"]
    return claimed


@pytest.mark.asyncio
async def test_update_fence_keeps_typed_reason_before_connect(refusal):
    with pytest.raises(gateway_client.GatewayUnavailableError) as caught:
        async with gateway_client.connect_gateway():
            pytest.fail("fenced gateway yielded a client")
    assert caught.value.state == "draining"
    assert caught.value.reason_code == "update_paused"
    refusal.assert_not_called()


@pytest.mark.parametrize("kind", ["recurring", "once"])
@pytest.mark.parametrize("prior_incident", [False, True])
def test_unadmitted_update_pause_preserves_obligations_without_outcome(
    tmp_path, monkeypatch, refusal, kind, prior_incident,
):
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    with jobs.use_cron_store(tmp_path):
        due = now() - timedelta(seconds=1)
        job = jobs.create_job(prompt="report", schedule=due.isoformat() if kind == "once" else "every 1h",
                              repeat=1 if kind == "once" else 3, deliver="slack:alerts")
        jobs.update_job(job["id"], {"next_run_at": due.isoformat()})
        job = jobs.get_job(job["id"])
        incident = incidents.upsert_incident(job["id"], "original failure") if prior_incident else None
        before_incidents = incidents.list_incidents()
        if kind == "recurring":
            # tick advances recurring slots before the worker receives its snapshot.
            jobs.advance_next_run(job["id"])
        attempt = _claim(job)
        expected_next = jobs.get_job(job["id"])["next_run_at"]
        with patch("cron.delivery_queue.enqueue", return_value={"status": "pending"}) as notice, \
             patch.object(scheduler, "_resolve_incidents_for_recovered_job") as recovery, \
             patch.object(scheduler, "save_job_output") as output:
            assert _run_body(attempt) is True
        stored = jobs.get_job(job["id"])
        assert incidents.list_incidents() == before_incidents
        notice.assert_not_called()
        recovery.assert_not_called()
        output.assert_not_called()
        assert stored["repeat"]["completed"] == 0
        assert stored.get("last_status") is None
        assert stored.get("last_run_at") is None
        assert stored.get("failure_streak", 0) == 0
        assert stored["enabled"] is True and stored["state"] == "scheduled"
        assert stored["next_run_at"] == expected_next
        assert not stored.get("fire_claim") and not stored.get("run_claim")
        assert executions.get_execution(attempt["execution_id"]) is None
        assert not scheduler_authority.journal_path(job["id"], attempt["execution_id"]).exists()
        assert stored["last_skip_reason"] == "update_paused"
        if kind == "once":
            # Even a long update must not retire a proved-unsubmitted one-shot as missed.
            monkeypatch.setattr(jobs, "_hermes_now", lambda: due + timedelta(minutes=10))
            assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]
        if incident:
            assert incidents.list_incidents() == before_incidents
    refusal.assert_not_called()


def test_reasonless_unavailable_is_still_a_failure(tmp_path, monkeypatch):
    from gateway import session_cron
    monkeypatch.setattr(session_cron, "owner_for_home", lambda _home: None)

    @asynccontextmanager
    async def connect():
        # Identical wording must not classify by text.
        raise gateway_client.GatewayUnavailableError("Gateway draining: update_paused", "draining")
        yield

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    with jobs.use_cron_store(tmp_path):
        result = scheduler_authority.run_canonical_job({"id": "job"}, execution_id="fire")
    assert result[0] is False
    assert "GatewayUnavailableError" in result[3]


@pytest.mark.parametrize("prior_journal", [False, True])
def test_post_submit_or_prior_journal_stays_unknown_without_replay(tmp_path, monkeypatch, prior_journal):
    from gateway import session_cron
    from utils import atomic_json_write
    monkeypatch.setattr(session_cron, "owner_for_home", lambda _home: None)
    calls = []

    class Peer:
        async def rpc(self, method, **params):
            calls.append(method)
            error = gateway_client.GatewayUnavailableError("Gateway draining: update_paused", "draining")
            error.reason_code = "update_paused"
            raise error

    @asynccontextmanager
    async def connect():
        yield Peer()

    # No reconnect waits: fake the transport seam, retain the real authority observe/journal logic.
    @asynccontextmanager
    async def transport(_params):
        if prior_journal:
            error = gateway_client.GatewayUnavailableError("Gateway draining: update_paused", "draining")
            error.reason_code = "update_paused"
            raise error
        peer = Peer()
        yield lambda op, data: peer.rpc("cron." + op, **data)

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    monkeypatch.setattr("cron.scheduler_transport.recovering_cron_client", transport)
    with jobs.use_cron_store(tmp_path):
        journal = scheduler_authority.journal_path("job", "fire")
        if prior_journal:
            atomic_json_write(journal, {"params": {"job_id": "job", "request_id": "fire", "extra_prompt": None},
                                        "receipt": None, "pid": 1, "process_started_at": 1, "owner_nonce": "prior"})
        with pytest.raises(scheduler_authority.CronExecutionUnknown):
            scheduler_authority.run_canonical_job({"id": "job"}, execution_id="fire")
        assert journal.exists()
        saved = json.loads(journal.read_text())
        assert saved["params"]["request_id"] == "fire"
        assert calls == ([] if prior_journal else ["cron.submit"])


def test_real_failure_still_opens_incident(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="report", schedule="every 1h", deliver="slack:alerts")
        jobs.update_job(job["id"], {"next_run_at": (now() - timedelta(seconds=1)).isoformat()})
        attempt = _claim(jobs.get_job(job["id"]))
        with patch.object(scheduler, "run_job", return_value=(False, "error", "", "real failure")), \
             patch("cron.delivery_queue.enqueue", return_value={"status": "pending"}) as notice:
            assert _run_body(attempt) is True
        assert len(incidents.list_incidents()) == 1
        assert jobs.get_job(job["id"])["last_status"] == "error"
        assert executions.get_execution(attempt["execution_id"])["status"] == "failed"
        notice.assert_called_once()


def test_detached_waiter_recognizes_only_exact_unadmitted_deferral(tmp_path, monkeypatch, refusal):
    from cron.scheduler_update_deferral import publish_worker_deferral
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    process = Mock()
    process.wait.return_value = process.poll.return_value = 0
    with jobs.use_cron_store(tmp_path):
        due = now() - timedelta(seconds=1)
        job = jobs.create_job(prompt="report", schedule=due.isoformat(), repeat=1, deliver="local")
        jobs.update_job(job["id"], {"next_run_at": due.isoformat()})
        attempt = _claim(jobs.get_job(job["id"]))
        execution_id = attempt["execution_id"]
        from cron.executions import adopt_claimed_execution, mark_execution_handoff_pending
        assert mark_execution_handoff_pending(execution_id)
        assert adopt_claimed_execution(execution_id)
        ack = tmp_path / (execution_id + ".ready")
        ack.write_text(json.dumps({"execution_id": execution_id}))
        monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", execution_id)
        assert scheduler.run_one_job(attempt) is True
        # Same payload dictionary crosses run_one_job and the existing ready-ack publisher.
        publish_worker_deferral(ack, attempt)
        assert json.loads(ack.read_text())["deferral_reason"] == "update_paused"
        assert scheduler._wait_for_external_cron_worker(
            process, execution_id=execution_id, handoff_files=(ack,),
        ) is True
    assert not ack.exists()


@pytest.mark.parametrize("returncode,receipt_id,reason,has_row", [
    (None, "fire", "update_paused", False),
    (1, "fire", "update_paused", False),
    (0, "other-fire", "update_paused", False),
    (0, "fire", "draining", False),
    (0, "fire", "update_paused", True),
])
def test_worker_deferral_receipt_cannot_hide_uncertain_or_real_failure(
    tmp_path, returncode, receipt_id, reason, has_row,
):
    from cron.scheduler_update_deferral import worker_deferred
    ack = tmp_path / "fire.ready"
    ack.write_text(json.dumps({"execution_id": receipt_id, "deferral_reason": reason}))
    process = Mock()
    process.poll.return_value = returncode
    with patch("cron.executions.get_execution", return_value={"status": "failed"} if has_row else None):
        assert worker_deferred(process, "fire", (ack,)) is False


def test_deferred_one_shot_runs_after_pause_and_clears_exemption(tmp_path, monkeypatch, refusal):
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    with jobs.use_cron_store(tmp_path):
        due = now() - timedelta(seconds=1)
        job = jobs.create_job(prompt="report", schedule=due.isoformat(), repeat=1, deliver="local")
        jobs.update_job(job["id"], {"next_run_at": due.isoformat()})
        job = jobs.get_job(job["id"])
        assert _run_body(_claim(job)) is True
        monkeypatch.setattr(jobs, "_hermes_now", lambda: due + timedelta(minutes=10))
        (pending,) = jobs.get_due_jobs()
        with patch.object(scheduler, "run_job", return_value=(True, "report", "report", None)) as run:
            assert _run_body(_claim(pending)) is True
        run.assert_called_once()
        stored = jobs.get_job(job["id"])
        assert stored["state"] == "completed"
        assert stored["repeat"]["completed"] == 1
        assert "update_deferred_slot" not in stored


def test_rearm_clears_deferral_even_for_same_instant(tmp_path):
    with jobs.use_cron_store(tmp_path):
        due = now() + timedelta(minutes=1)
        job = jobs.create_job(prompt="report", schedule=due.isoformat())
        jobs.update_job(job["id"], {"update_deferred_slot": job["next_run_at"]})
        rearmed = jobs.rearm_oneshot(job["id"], due.isoformat())
        assert "update_deferred_slot" not in rearmed
