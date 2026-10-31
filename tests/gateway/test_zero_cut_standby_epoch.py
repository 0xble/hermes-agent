"""A zero-cut standby begins no runtime epoch until promotion.

Slot b boots beside slot a, which still serves and owns the store's epoch. A begin at b's boot staled
every write of a and its startup recovery turned a's running turns ``unknown``. The standby now builds
its authorities unclaimed (epoch 0, every fenced write refused) and claims at promotion.
"""
import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import gateway_slot, zero_cut_standby as zcs
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.run_runtime import claim_gateway_runtime, initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session_authority import UNCLAIMED_EPOCH
from hermes_state import SessionDB
from tests.fakes.runtime_owner import begin_runtime_epoch


@pytest.fixture
def standby_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    monkeypatch.setattr(gateway_slot, "_query_other_slot",
                        lambda home, slot, timeout: {"standby": False} if slot == "a" else None)
    gateway_slot.reset_gateway_slot_for_tests()  # a slot pinned by an earlier test would win
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    zcs.reset_for_tests()
    # Slot a's epoch, begun before the standby existed.
    db = SessionDB(db_path=home / "state.db")
    serving_epoch = begin_runtime_epoch(db, instance_id="slot-a")
    db.close()
    yield home, serving_epoch
    zcs.reset_for_tests()
    gateway_slot.reset_gateway_slot_for_tests()


def _store_epoch(home: Path):
    db = SessionDB(db_path=home / "state.db")
    try:
        with db._read_ctx() as conn:
            return tuple(conn.execute("SELECT epoch, instance_id FROM runtime_epoch").fetchone())
    finally:
        db.close()


@pytest.mark.asyncio
async def test_standby_boot_leaves_the_serving_epoch_and_promotion_claims_it(standby_home):
    home, serving_epoch = standby_home
    assert zcs.is_standby()
    runner = GatewayRunner(GatewayConfig())
    process_ownership.reserve([home])
    try:
        await initialize_gateway_runtime(runner)
        authority = runner.session_authority
        assert authority.epoch == UNCLAIMED_EPOCH
        assert _store_epoch(home) == (serving_epoch, "slot-a"), "the standby took slot a's epoch at boot"

        await claim_gateway_runtime(runner)
        instance = runner.session_runtime_descriptor["instance_id"]
        assert _store_epoch(home) == (serving_epoch + 1, instance)
        assert authority.epoch == serving_epoch + 1
        assert runner.session_runtime_descriptor["authority_epoch"] == serving_epoch + 1
        assert runner.session_store._local_authority_epoch == serving_epoch + 1

        await claim_gateway_runtime(runner)  # a retried promotion claims nothing twice
        assert _store_epoch(home) == (serving_epoch + 1, instance)
    finally:
        for authority in list(getattr(runner, "session_authorities", []) or []):
            authority.db.close()
        process_ownership.release(home)



def _multiplexed_standby_runner(home, monkeypatch):
    """A standby runner serving the launch home plus secondary profile ``guest``."""
    guest = home / "profiles" / "guest"
    guest.mkdir(parents=True, mode=0o700)
    (guest / "config.yaml").write_text("{}")
    db = SessionDB(db_path=guest / "state.db")
    begin_runtime_epoch(db, instance_id="slot-a")
    db.close()
    config = GatewayConfig()
    config.multiplex_profiles = True
    config._runtime_profile_homes = (("default", home), ("guest", guest))
    return GatewayRunner(config), guest


@pytest.mark.asyncio
async def test_a_secondary_claim_failure_parks_it_and_promotion_still_serves(standby_home, monkeypatch):
    """Past the launch claim slot a is stale: a secondary that cannot claim is parked, as boot parks
    one, and the promotion completes instead of rolling back with nothing serving."""
    home, serving_epoch = standby_home
    runner, guest = _multiplexed_standby_runner(home, monkeypatch)
    process_ownership.reserve([home, guest])
    try:
        await initialize_gateway_runtime(runner)
        from gateway import session_authority
        real_claim = session_authority.claim_session_authority

        async def claim(authority, **kwargs):
            if Path(authority.db.db_path).resolve().parent == guest.resolve():
                raise RuntimeError("guest store is unusable")
            return await real_claim(authority, **kwargs)
        monkeypatch.setattr(session_authority, "claim_session_authority", claim)

        runner._served_profile_homes = {"default": home, "guest": guest}  # recorded at boot
        await claim_gateway_runtime(runner)
        instance = runner.session_runtime_descriptor["instance_id"]
        assert _store_epoch(home) == (serving_epoch + 1, instance)
        assert runner.session_runtime_descriptor["authority_epoch"] == serving_epoch + 1
        assert guest.resolve() not in runner.session_authorities
        assert not process_ownership.owns(guest)
        from gateway.run_runtime import parked_profile_map
        assert "guest" in parked_profile_map(runner)
        assert "guest" not in runner._served_profile_homes

        # Promotion then starts deferred secondaries from the served set: never the parked one.
        started, statuses = [], []
        runner._start_one_profile_adapters = lambda profile, home, claimed: asyncio.sleep(
            0, result=started.append(profile))
        runner._update_platform_runtime_status = lambda key, **fields: statuses.append(key)
        failed = []
        await zcs._promote_secondary_profiles(runner, ["guest"], failed)
        assert started == [], "a parked profile got adapters with no authority"
        assert failed == ["guest:telegram"]
    finally:
        for authority in list(getattr(runner, "session_authorities", []) or []):
            authority.db.close()
        process_ownership.release(home)
        process_ownership.release(guest)


@pytest.mark.asyncio
async def test_a_retried_promotion_finishes_a_half_claimed_authority(standby_home, monkeypatch):
    """A claim that raised after its begin is resumed by the retry: the begun epoch is reused (no
    second bump) and the store's local-reset fence ends up set."""
    home, serving_epoch = standby_home
    runner = GatewayRunner(GatewayConfig())
    process_ownership.reserve([home])
    try:
        await initialize_gateway_runtime(runner)
        from gateway import session_local_adoption as adoption
        real_adopt, failures = adoption.adopt_local_compressions, []

        async def adopt(authority):
            if not failures:
                failures.append(authority)
                raise OSError("disk hiccup")
            return await real_adopt(authority)
        monkeypatch.setattr(adoption, "adopt_local_compressions", adopt)

        with pytest.raises(OSError):
            await claim_gateway_runtime(runner)
        await claim_gateway_runtime(runner)  # promote()'s rollback lets the control client retry
        instance = runner.session_runtime_descriptor["instance_id"]
        assert _store_epoch(home) == (serving_epoch + 1, instance), "the retry began a second epoch"
        assert getattr(runner.session_store, "_local_authority_epoch", None) == serving_epoch + 1, \
            "the retry returned early and left the local-reset fence unset"
        assert runner.session_authority.epoch_claimed is True
    finally:
        for authority in list(getattr(runner, "session_authorities", []) or []):
            authority.db.close()
        process_ownership.release(home)


_HOLDER = r"""
import fcntl, sys
handle = open(sys.argv[1], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.read()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock stand-in for slot a")
@pytest.mark.parametrize("held, standby", [(True, True), (False, False)])
def test_an_unanswered_role_query_is_standby_while_the_sibling_holds_its_lock(
        tmp_path, monkeypatch, held, standby):
    """A slow or wedged slot a that still holds its lock must not make slot b boot active."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_slot, "_query_other_slot", lambda home, slot, timeout: None)
    config = {"gateway": {"zero_cut_handover": {"enabled": True}}}
    holder = None
    if held:
        holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(tmp_path / "gateway.lock")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert holder.stdout.readline().strip() == "held"
    try:
        assert gateway_slot._resolve_gateway_role("b", config) is standby
    finally:
        if holder is not None:
            holder.stdin.close()
            holder.wait(timeout=30)


@pytest.mark.asyncio
async def test_a_standby_serves_and_rescans_no_profile(standby_home):
    """``serve-profile`` and ``rescan-profiles`` would build a profile's authority and begin its
    epoch under the serving slot; a standby refuses both until promotion."""
    from gateway.run_profile_reconcile import serve_profile_verb
    runner = GatewayRunner(GatewayConfig())
    result = await runner.reconcile_served_profiles(reason="control-socket")
    assert result["standby"] is True and result["added"] == []
    verb = serve_profile_verb(runner)
    answer = await asyncio.to_thread(verb, {"name": "guest"})
    assert "standby" in answer["error"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs an unopenable POSIX lock")
async def test_an_unopenable_predecessor_lock_does_not_stall_adoption(tmp_path, monkeypatch):
    """A predecessor lock this user cannot open (a stale root-owned inode) never reads free;
    adoption goes on to the host-role claim (which has its own lock) instead of polling forever."""
    calls = []
    monkeypatch.setattr(zcs, "_PREDECESSOR_POLL_S", 0.01)
    monkeypatch.setattr("gateway.status._get_process_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.run._claim_host_gateway_role", lambda force=False: calls.append("claim"))
    monkeypatch.setattr("gateway.run._refresh_host_gateway_record", lambda r: calls.append("record"))
    monkeypatch.setattr("gateway.gateway_slot.effective_gateway_slot", lambda config=None: "b")
    lock = tmp_path / "gateway.lock"  # this process is slot b; its predecessor is slot a
    lock.touch()
    lock.chmod(0)
    try:
        await asyncio.wait_for(
            zcs._adopt_predecessor_duties(SimpleNamespace(config=SimpleNamespace(durable_outbox_enabled=False))), 5)
    finally:
        lock.chmod(0o600)
    assert calls == ["claim", "record"]



@pytest.mark.asyncio
async def test_a_retried_promotion_reclaims_an_epoch_taken_since_its_claim(standby_home):
    """A promotion that claimed and then rolled back leaves slot b answering standby while slot a
    restarts and begins a newer epoch. The retry must claim again, not serve on its old epoch."""
    home, serving_epoch = standby_home
    runner = GatewayRunner(GatewayConfig())
    process_ownership.reserve([home])
    try:
        await initialize_gateway_runtime(runner)
        await claim_gateway_runtime(runner)
        # Slot a restarts (it may: slot b answers standby) and begins its own epoch.
        conn = sqlite3.connect(home / "state.db")
        with conn:
            conn.execute("UPDATE runtime_epoch SET epoch=epoch+1, instance_id='slot-a-restarted'")
        conn.close()
        await claim_gateway_runtime(runner)
        instance = runner.session_runtime_descriptor["instance_id"]
        assert _store_epoch(home) == (serving_epoch + 3, instance)
        assert runner.session_authority.epoch == serving_epoch + 3, "slot b serves on a stale epoch"
        assert runner.session_runtime_descriptor["authority_epoch"] == serving_epoch + 3
    finally:
        for authority in list(getattr(runner, "session_authorities", []) or []):
            authority.db.close()
        process_ownership.release(home)


@pytest.mark.parametrize("error, state", [
    (OSError(24, "Too many open files"), gateway_slot.LOCK_HELD),
    (PermissionError(13, "Permission denied"), gateway_slot.LOCK_UNKNOWN),
], ids=["emfile", "eacces"])
def test_only_a_permission_error_makes_a_lock_unknown(tmp_path, monkeypatch, error, state):
    """EMFILE is transient and says nothing about the holder: a waiter keeps polling."""
    (tmp_path / "gateway.lock").touch()

    def refuse(*_a, **_k):
        raise error
    monkeypatch.setattr(gateway_slot, "open", refuse, raising=False)
    assert gateway_slot.slot_lock_state(tmp_path, "a") == state


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs an unopenable POSIX lock")
async def test_adoption_waits_on_an_unopenable_lock_while_the_predecessor_answers(tmp_path, monkeypatch):
    """An unopenable predecessor lock is not an exit: adoption waits while that slot still answers."""
    calls, answering = [], [True]
    monkeypatch.setattr(zcs, "_PREDECESSOR_POLL_S", 0.01)
    monkeypatch.setattr(zcs, "_slot_answers", lambda home, slot: answering[0], raising=False)
    monkeypatch.setattr("gateway.status._get_process_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.gateway_slot.effective_gateway_slot", lambda config=None: "b")
    monkeypatch.setattr("gateway.run._claim_host_gateway_role", lambda force=False: calls.append("claim"))
    monkeypatch.setattr("gateway.run._refresh_host_gateway_record", lambda r: calls.append("record"))
    lock = tmp_path / "gateway.lock"
    lock.touch()
    lock.chmod(0)
    try:
        task = asyncio.create_task(
            zcs._adopt_predecessor_duties(SimpleNamespace(config=SimpleNamespace(durable_outbox_enabled=False))))
        await asyncio.sleep(0.2)
        assert calls == [], "adopted the host role while slot a still answers"
        answering[0] = False  # slot a exits
        await asyncio.wait_for(task, 5)
    finally:
        lock.chmod(0o600)
    assert calls == ["claim", "record"]
