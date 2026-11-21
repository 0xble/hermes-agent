"""Standalone ``serve`` (no session authority) must not run a legacy turn past the gateway's
durable ledger: an ``unknown`` admission is never replayed, queued input runs in FIFO order."""

from tests.tui_gateway._thread_fixtures import patch_server_threads
import contextlib
import types

import pytest

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import conversation
<<<<<<< HEAD
from hermes_state_runtime import admit_session_input, claim_session_input, list_session_admissions, recover_session_inputs, resolve_unknown_session_input
||||||| dff103e05878
from hermes_state_runtime import (
    admit_session_input, begin_runtime_epoch, claim_session_input, list_session_admissions,
    recover_session_inputs, resolve_unknown_session_input,
)
=======
from tests.fixtures.storage_seed import seed_session
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
from tests.fakes.runtime_owner import begin_runtime_epoch
from tui_gateway import server

pytestmark = pytest.mark.usefixtures("without_write_stall_sampler")


class _InlineThread:
    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.mark.parametrize("ledger", ["unknown", "queued"])
def test_legacy_submit_refuses_while_canonical_ledger_holds_work(monkeypatch, tmp_path, ledger):
    db = open_persistence(profile_scope(home=tmp_path))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    for name in ("_schedule_agent_build", "_schedule_session_cap_enforcement", "_register_session_cwd",
                 "_start_agent_build"):
        monkeypatch.setattr(server, name, lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *_a: False)
    turns = []
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **kw: turns.append(a[1]))
    patch_server_threads(monkeypatch, server, _InlineThread)
    created = server.handle_request({"id": "c", "method": "session.create", "params": {"source": "desktop"}})
    sid, key = created["result"]["session_id"], created["result"]["stored_session_id"]
    seed_session(db, key, "cli")
    first = db.runtime.begin_epoch(instance_id="gateway-1")
    db.admissions.admit(epoch=first, principal_id="owner", session_id=key, request_id="r1",
                        payload={"text": "lost?"})
    epoch = first
    if ledger == "unknown":
        db.admissions.claim(epoch=first, session_id=key)
        epoch = db.runtime.begin_epoch(instance_id="gateway-2")
        db.admissions.recover(epoch=epoch)

    def submit():
        return server.handle_request({"id": "p", "method": "prompt.submit", "params": {"session_id": sid, "text": "hi"}})

    try:
        refused = submit()
        assert refused["error"]["code"] == 4090, refused
        assert refused["error"]["data"] == {"reason": "SESSION_NOT_OWNED"}
        assert turns == [] and conversation(db, key) == []
        if ledger == "unknown":
            # Once the operator discards the lost turn through the gateway the barrier lifts.
            (row,) = db.admissions.list(session_id=key)
            db.admissions.resolve_unknown(epoch=epoch, admission_id=row.admission_id, generation=row.generation)
            assert submit()["result"]["status"] == "streaming"
            assert turns == [sid]
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_ledger_read_failure_refuses_as_coordination_unavailable(monkeypatch):
    """A store failure reading the ledger refuses the turn rather than running it past unknown work."""
    from hermes_cli.active_sessions import SESSION_COORDINATION_UNAVAILABLE
    from persistence.core.errors import TransientStoreError

    def _failing(_key):
        raise TransientStoreError("disk I/O error")

    handle = types.SimpleNamespace(runtime=types.SimpleNamespace(pending_ledger_status=_failing))
    monkeypatch.setattr(server, "_session_db", lambda _session: contextlib.nullcontext(object()))
    monkeypatch.setattr(server, "_persistence_for", lambda _db: contextlib.nullcontext(handle))
    refusal = server._canonical_ledger_refusal({"session_key": "stored"})
    assert refusal is not None and refusal.reason == SESSION_COORDINATION_UNAVAILABLE
