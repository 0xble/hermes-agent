"""Forward-only authority against real SQLite transactions and process identities."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import time

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity, check_poller_journal



def pair(home):
    db = GenerationCoordinator(home)
    old = GenerationIdentity.create(release_sha="old", label="old",
        boot_id="fixture-boot", start_fingerprint=f"{os.getpid()}:fixture")
    new = db.reserve_generation(release_sha="new", label="new", boot_id="fixture-boot")
    assert db.claim_generation(new.id, os.getpid(), old.start_fingerprint, boot_id="fixture-boot")
    db.register(old, state="serving")
    return db, old, new, db.acquire_lease("active_generation", old.id)


@pytest.mark.parametrize("state", ["draining", "exited"])
@pytest.mark.parametrize("destination", ["standby", "serving"])
def test_runtime_is_forward_only(tmp_path, state, destination):
    db, old, new, epoch = pair(tmp_path)
    db.request_transfer(old.id, new.id, epoch, set())
    db.commit_transfer(old.id, new.id, epoch)
    if state == "exited":
        db.heartbeat(old.id, state="exited")
    with pytest.raises(RuntimeError):
        db.heartbeat(old.id, state=destination)
    with pytest.raises(RuntimeError):
        db.acquire_lease("new-resource", old.id)


def test_retirement_verdict_survives_heartbeat_and_direct_sql(tmp_path):
    db = GenerationCoordinator(tmp_path)
    reserved = db.reserve_generation(release_sha="r", label="r", boot_id="fixture-boot")
    assert db.retire_unclaimed(reserved.id)
    db.heartbeat(reserved.id, state="exited")
    assert not db.claim_generation(reserved.id, os.getpid(), "fingerprint", boot_id="fixture-boot")
    with closing(db.connect()) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE generations SET verdict=NULL WHERE id=?", (reserved.id,))
    row = db.generations()[0]
    assert (row["state"], row["verdict"], row["verdict_evidence"]) == ("exited", "failed", "unclaimed")


def test_claim_is_one_shot_and_label_stays_reserved(tmp_path):
    db = GenerationCoordinator(tmp_path)
    reservation = db.reserve_generation(release_sha="r", label="unique", boot_id="fixture-boot")
    # No process claimed before this point, a first respawn can still win.
    with ThreadPoolExecutor(max_workers=8) as workers:
        wins = list(workers.map(lambda i: db.claim_generation(reservation.id, i + 100, f"start-{i}", boot_id="fixture-boot"), range(16)))
    assert sum(wins) == 1
    claimed = db.generations()[0]
    assert not db.claim_generation(reservation.id, 123456, "after-crash", boot_id="fixture-boot")
    assert db.generations()[0]["pid"] == claimed["pid"]
    assert not db.retire_unclaimed(reservation.id)
    with pytest.raises(sqlite3.IntegrityError):
        db.reserve_generation(release_sha="other", label="unique", boot_id="fixture-boot")


@pytest.mark.parametrize('claimed', [False, True])
def test_sql_rejects_two_live_forward_only_rows_for_same_label(tmp_path, claimed):
    db = GenerationCoordinator(tmp_path)
    first = db.reserve_generation(release_sha='release', label='label', boot_id='fixture-boot')
    if claimed:
        assert db.claim_generation(first.id, os.getpid(), 'fingerprint', boot_id='fixture-boot')
    # Bypass the reservation helper to prove the database fence itself, both
    # before and after claim_pending is cleared by the first process.
    with closing(db.connect()) as conn, pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        conn.execute("INSERT INTO generations "
            "(id,release_sha,label,pid,started_at,boot_id,start_fingerprint,state,heartbeat_at,claim_pending,claim_at) "
            "SELECT 'collision',release_sha,label,pid,started_at,boot_id,start_fingerprint,state,heartbeat_at,"
            "0,NULL FROM generations WHERE id=?", (first.id,))
    assert len(db.generations()) == 1


def test_suspect_and_unknown_live_fingerprint_cannot_move_lease(tmp_path, monkeypatch):
    db, old, new, epoch = pair(tmp_path)
    db.observe_suspect(old.id)
    monkeypatch.setattr("gateway.generation._boot_id", lambda: "fixture-boot")
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: None)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    calls = []
    with pytest.raises(RuntimeError, match="death proof"):
        db.takeover_dead_generation("active_generation", old.id, new.id,
                                    bootout=lambda label: calls.append(label))
    assert calls == []
    assert next(row for row in db.generations() if row["id"] == old.id)["state"] == "serving"
    assert db.leases()[0]["epoch"] == epoch
    with pytest.raises(RuntimeError):
        db.acquire_lease("active_generation", new.id)


@pytest.mark.parametrize("hook_result", [True, False, None])
def test_takeover_order_failure_and_retired_retry(tmp_path, hook_result):
    db, old, new, epoch = pair(tmp_path)
    events = []
    def proof(row):
        events.append("proof")
        assert row["pid"] == old.pid
        return True
    def bootout(label):
        events.append("bootout")
        retired = next(row for row in db.generations() if row["id"] == old.id)
        assert (retired["verdict"], retired["state"]) == ("failed", "exited")
        assert db.leases()[0]["epoch"] == epoch
        return hook_result
    if hook_result is True:
        assert db.takeover_dead_generation("active_generation", old.id, new.id,
            bootout=bootout, death_proof=proof) == epoch + 1
    else:
        with pytest.raises(RuntimeError, match="bootout"):
            db.takeover_dead_generation("active_generation", old.id, new.id,
                bootout=bootout, death_proof=proof)
        assert db.leases()[0]["epoch"] == epoch
        assert db.takeover_dead_generation("active_generation", old.id, new.id,
            bootout=lambda label: True, death_proof=proof) == epoch + 1
    assert events[:2] == ["proof", "bootout"]


def test_takeover_revalidates_epoch_after_hook(tmp_path):
    db, old, new, epoch = pair(tmp_path)
    def race(label):
        with closing(db.connect()) as conn:
            db.release_lease("active_generation", old.id, epoch)
        return True
    with pytest.raises(RuntimeError, match="changed"):
        db.takeover_dead_generation("active_generation", old.id, new.id,
            bootout=race, death_proof=lambda row: True)
    assert db.leases()[0]["generation_id"] == old.id


def test_journal_gap_overlap_crash_and_malformed_evidence(tmp_path):
    db, old, new, epoch = pair(tmp_path)
    def event(owner, event, at):
        db.record_poller_event("hash", owner, epoch, event, wall_at=at, monotonic_at=at)
    for owner, base in ((old.id, 0), (new.id, 12)):
        for name, offset in (("lock_acquired", 1), ("poller_started", 2),
                             ("poller_stopped", 10), ("lock_released", 11)):
            event(owner, name, base + offset)
    result = db.check_poller_journal()
    assert result["ok"] and result["longest_zero_poller_gap"] == 4
    with closing(db.connect()) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM poller_journal")
    assert not check_poller_journal([])["ok"]
    assert not check_poller_journal([{"event": "poller_started"}])["ok"]
    event(old.id, "lock_acquired", 25)
    event(old.id, "poller_started", 26)
    assert not db.check_poller_journal()["ok"]  # Crash without a stop is unknown.
    event(new.id, "lock_acquired", 27)
    event(new.id, "poller_started", 28)
    assert any(v["reason"] == "overlapping_poller" for v in db.check_poller_journal()["violations"])


def old_process(tmp_path, home, program):
    export = tmp_path / "old"
    package = export / "gateway"
    package.mkdir(parents=True, exist_ok=True)
    fixtures = Path(__file__).parent / "fixtures"
    for name in ("generation", "owned_admission"):
        shutil.copyfile(fixtures / f"{name}_738c502c.py", package / f"{name}.py")
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(export)}
    result = subprocess.run([sys.executable, "-c", program], cwd=export, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "OLD_READ_WRITE_OK" in result.stdout


def test_previous_release_really_reads_and_writes_new_database_copy(tmp_path):
    home = tmp_path / "new"
    db, old, new, epoch = pair(home)
    historical = db.reserve_generation(release_sha="retired", label="retired",
        started_at=time.time() - 8 * 86400, boot_id="fixture-boot")
    assert db.retire_unclaimed(historical.id)
    copy = tmp_path / "copy"
    copy.mkdir()
    with closing(db.connect()) as source, closing(sqlite3.connect(copy / db.path.name)) as target:
        source.backup(target)
    old_process(tmp_path, copy, '''
from pathlib import Path
import os
from gateway.generation import GenerationCoordinator, GenerationIdentity
c = GenerationCoordinator(Path(os.environ['HERMES_HOME']))
assert len(c.generations()) == 3
assert c.leases()[0]['epoch'] == 1
row = GenerationIdentity.create(release_sha='legacy', label='legacy-write', boot_id='fixture-boot')
c.register(row, state='serving')
c.heartbeat(row.id)
assert c.acquire_lease('legacy-resource', row.id) == 1
assert c.release_lease('legacy-resource', row.id, 1)
assert row.id in {record['id'] for record in c.generations()}
assert next(record for record in c.generations() if record['label'] == 'retired')['verdict'] == 'failed'
try:
    c.acquire_lease('legacy-resource', next(record['id'] for record in c.generations() if record['release_sha'] == 'old'))
except Exception as exc:
    assert 'handover or takeover' in str(exc)
else:
    raise AssertionError('old acquire bypassed lease authority')
print('OLD_READ_WRITE_OK')
''')
    reopened = GenerationCoordinator(copy)
    assert len(reopened.generations()) == 4
    assert next(row for row in reopened.generations() if row["id"] == historical.id)["verdict"] == "failed"
    assert next(row for row in reopened.generations() if row["id"] == old.id)["state"] == "serving"


def test_legacy_duplicate_labels_and_failed_state_are_preserved(tmp_path):
    home = tmp_path / "legacy"
    old_process(tmp_path, home, '''
from pathlib import Path
import os
from gateway.generation import GenerationCoordinator, GenerationIdentity
c = GenerationCoordinator(Path(os.environ['HERMES_HOME']))
for state in ('failed', 'ready'):
    c.register(GenerationIdentity.create(release_sha='old', label='duplicate', boot_id='fixture-boot'), state=state)
active = GenerationIdentity.create(release_sha='old', label='active', boot_id='fixture-boot')
c.register(active, state='ready')
c.acquire_lease('active_generation', active.id)
print('OLD_READ_WRITE_OK')
''')
    db = GenerationCoordinator(home)
    assert len(db.generations()) == 3
    assert {row["state"] for row in db.generations()} == {"exited", "standby", "serving"}
    assert next(row for row in db.generations() if row["label"] == "active")["state"] == "serving"
    assert next(row for row in db.generations() if row["state"] == "exited")["verdict"] == "failed"
    with pytest.raises(sqlite3.IntegrityError):
        db.reserve_generation(release_sha="new", label="duplicate", boot_id="fixture-boot")


@pytest.fixture(autouse=True)
def fixture_boot(monkeypatch):
    monkeypatch.setattr("gateway.generation._boot_id", lambda: "fixture-boot")


def test_suspect_blocks_cooperative_transfer_and_abort_requires_serving(tmp_path):
    db, old, new, epoch = pair(tmp_path)
    db.request_transfer(old.id, new.id, epoch, set())
    nonce = db.transfer_attempt_nonce(old.id, epoch)
    db.observe_suspect(old.id, evidence="expired")
    with pytest.raises(RuntimeError, match="suspect"):
        db.commit_transfer(old.id, new.id, epoch)
    db.heartbeat(old.id)
    db.commit_transfer(old.id, new.id, epoch)
    with pytest.raises(RuntimeError):
        db.abort_transfer(old.id, new.id, epoch, attempt_nonce=nonce)
    assert next(row for row in db.generations() if row["id"] == old.id)["state"] == "draining"


def test_journal_missing_poller_and_regressing_monotonic_fail_closed(tmp_path):
    db, old, new, epoch = pair(tmp_path)
    db.record_poller_event("hash", old.id, epoch, "lock_acquired", wall_at=1, monotonic_at=5)
    db.record_poller_event("hash", old.id, epoch, "lock_released", wall_at=2, monotonic_at=4)
    reasons = {row["reason"] for row in db.check_poller_journal()["violations"]}
    assert {"missing_poller_evidence", "clock_regression"} <= reasons
    assert not check_poller_journal([None])["ok"]


def test_claimed_identity_and_unclaimed_lease_are_fenced_in_sql(tmp_path):
    db, old, new, epoch = pair(tmp_path)
    reserved = db.reserve_generation(release_sha="third", label="third")
    with closing(db.connect()) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE generations SET pid=pid+1 WHERE id=?", (new.id,))
        with pytest.raises(sqlite3.IntegrityError, match="claimed"):
            conn.execute("INSERT INTO leases VALUES('bypass',1,?,'active')", (reserved.id,))
        with pytest.raises(sqlite3.IntegrityError, match="handover or takeover"):
            conn.execute("UPDATE leases SET generation_id=?,epoch=epoch+1", (new.id,))


def test_released_lease_still_requires_ordered_proven_dead_takeover(tmp_path):
    db, old, new, epoch = pair(tmp_path)
    assert db.release_lease("active_generation", old.id, epoch)
    db.heartbeat(old.id, state="exited")
    with pytest.raises(RuntimeError):
        db.acquire_lease("active_generation", new.id)
    with pytest.raises(RuntimeError, match="death proof"):
        db.takeover_dead_generation("active_generation", old.id, new.id,
            bootout=lambda label: True, death_proof=lambda row: False)
    assert db.takeover_dead_generation("active_generation", old.id, new.id,
        bootout=lambda label: True, death_proof=lambda row: True) == epoch + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["draining", "exited"])
async def test_direct_poller_rearm_refuses_forward_only_owner(tmp_path, state):
    from gateway.run_generation import ActiveGeneration
    db, old, new, epoch = pair(tmp_path)
    db.request_transfer(old.id, new.id, epoch, set())
    db.commit_transfer(old.id, new.id, epoch)
    if state == "exited":
        db.heartbeat(old.id, state="exited")
    active = ActiveGeneration(tmp_path, db, old, epoch)
    with pytest.raises(RuntimeError, match="still-serving"):
        await active._rearm_stopped_pollers()


def test_direct_process_claim_loser_exits_zero_without_new_resources(tmp_path):
    from gateway.run_generation import _claim_process_generation
    db = GenerationCoordinator(tmp_path)
    reserved = db.reserve_generation(release_sha="r", label="one-shot")
    first = GenerationIdentity.create(release_sha="r", label="one-shot", start_fingerprint="first")
    assert _claim_process_generation(db, first).id == reserved.id
    respawn = GenerationIdentity.create(release_sha="r", label="one-shot", start_fingerprint="respawn")
    with pytest.raises(SystemExit) as refusal:
        _claim_process_generation(db, respawn)
    assert refusal.value.code == 0
    assert len(db.generations()) == 1 and db.leases() == []
    assert db.generations()[0]["start_fingerprint"] == "first"
