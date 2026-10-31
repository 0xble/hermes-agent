"""A zero-cut standby begins no runtime epoch until promotion.

Slot b boots beside slot a, which still serves and owns the store's epoch. A begin at b's boot staled
every write of a and its startup recovery turned a's running turns ``unknown``. The standby now builds
its authorities unclaimed (epoch 0, every fenced write refused) and claims at promotion.
"""
from pathlib import Path

import pytest

from gateway import gateway_slot, zero_cut_standby as zcs
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.run_runtime import claim_gateway_runtime, initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session_authority import UNCLAIMED_EPOCH
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


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
