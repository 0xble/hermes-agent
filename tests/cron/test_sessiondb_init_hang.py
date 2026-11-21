"""Regression test for a hung SessionDB() init permanently wedging a cron job.

Real-world incident: a cron job's ``SessionDB()`` construction inside
``run_job`` blocked forever (a wedged sqlite3.connect against state.db, no
other process holding a competing lock by the time it was diagnosed). Because
that call had no timeout of its own — unlike the agent's run_conversation,
which is already bounded by HERMES_CRON_TIMEOUT — the worker thread submitted
by ``_submit_with_guard`` never returned. Its ``finally`` block, which is the
only thing that discards the job ID from ``_running_job_ids``, never ran.
Every later tick logged "already running — skipping" and the job never fired
again until the whole gateway process was restarted days later.

These tests prove ``run_job`` now bounds the SessionDB init with its own
timeout (HERMES_CRON_SESSION_DB_TIMEOUT, default 10s per attempt) so a hang there can
never again wedge the job past that bound, and — end to end — that the
dispatch guard is released and the job becomes dispatchable again afterward.

Assertions capture the timeout passed to ``Future.result(timeout=...)`` (and
optionally force an immediate ``TimeoutError``) — no wall-clock waits, so the
suite stays free of timing flakes under parallel load.
"""

import concurrent.futures
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import run_job

# Hold the real class: patching cron.scheduler.concurrent.futures.ThreadPoolExecutor
# also replaces concurrent.futures.ThreadPoolExecutor (same module object).
_REAL_TPE = concurrent.futures.ThreadPoolExecutor

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


<<<<<<< HEAD
def _run_owned_job(job, home, db, *, execution_id=None, **kwargs):
    """Exercise run_job under the production owner-execution binding, not its RPC client."""
    from types import SimpleNamespace
    from gateway import session_cron
||||||| dff103e05878
@pytest.fixture
def owner_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = MagicMock(db_path=tmp_path / "state.db")
    db.get_compression_tip.return_value = None
    owner = SimpleNamespace(db=db)
    job = {"id": "owner-db", "name": "test", "prompt": "hello"}
    execution_id = "owner-fire"
    token = session_cron._execution.set((owner, "owner-session", job["id"], execution_id))
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    monkeypatch.setattr(scheduler, "_reload_dotenv_and_publish_delivery_target", lambda *_: None)
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup", lambda *_: scheduler._CronAgentSetup(
        model="test", runtime={"provider": "test"}))
    with patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.return_value = {"final_response": "ok"}
        try:
            yield owner, job, execution_id, agent_cls
        finally:
            session_cron._execution.reset(token)
=======
@pytest.fixture
def owner_run(tmp_path, monkeypatch):
    """The owner holds a handle on its profile's store; ``owner.db`` is the handle cron opens on it."""
    from persistence_assembly import profile_scope

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = MagicMock()
    db.lineage.tip.return_value = None
    owner = SimpleNamespace(db=db, persistence=MagicMock(scope=profile_scope(home=tmp_path)))
    job = {"id": "owner-db", "name": "test", "prompt": "hello"}
    execution_id = "owner-fire"
    token = session_cron._execution.set((owner, "owner-session", job["id"], execution_id))
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    monkeypatch.setattr(scheduler, "_reload_dotenv_and_publish_delivery_target", lambda *_: None)
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup", lambda *_: scheduler._CronAgentSetup(
        model="test", runtime={"provider": "test"}))
    with patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.return_value = {"final_response": "ok"}
        try:
            yield owner, job, execution_id, agent_cls
        finally:
            session_cron._execution.reset(token)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

<<<<<<< HEAD
    db.db_path = home / "state.db"
    token = session_cron.bind_execution(
        SimpleNamespace(db=db), "canonical-cron-session", job["id"], execution_id)
||||||| dff103e05878

def test_sessiondb_init_preserves_multiplex_profile_context(owner_run, tmp_path):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    owner, job, execution_id, agent_cls = owner_run
    profile_home = tmp_path / "profiles" / "jobsearch"
    owner.db.db_path = profile_home / "state.db"
    observed = []

    def acquire(path):
        observed.append((path, get_hermes_home()))
        return owner.db

    token = set_hermes_home_override(profile_home)
=======

def test_sessiondb_init_preserves_multiplex_profile_context(owner_run, tmp_path):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from persistence_assembly import profile_scope

    owner, job, execution_id, agent_cls = owner_run
    profile_home = tmp_path / "profiles" / "jobsearch"
    owner.persistence.scope = profile_scope(home=profile_home)
    observed = []

    def acquire(scope, *, eager=False):
        observed.append((scope, eager, get_hermes_home()))
        return owner.db

    token = set_hermes_home_override(profile_home)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    try:
<<<<<<< HEAD
        return run_job(job, execution_id=execution_id, **kwargs)
||||||| dff103e05878
        with patch("hermes_state_registry.acquire", side_effect=acquire):
            success, _, response, error = scheduler.run_job(job, execution_id=execution_id)
=======
        with patch("persistence_assembly.open_persistence", side_effect=acquire):
            success, _, response, error = scheduler.run_job(job, execution_id=execution_id)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    finally:
<<<<<<< HEAD
        session_cron._execution.reset(token)
||||||| dff103e05878
        reset_hermes_home_override(token)
    assert (success, response, error) == (True, "ok", None)
    assert observed == [(profile_home / "state.db", profile_home)]
    assert agent_cls.call_args.kwargs["session_db"] is owner.db
=======
        reset_hermes_home_override(token)
    assert (success, response, error) == (True, "ok", None)
    assert observed == [(owner.persistence.scope, True, profile_home)]
    assert agent_cls.call_args.kwargs["session_db"] is owner.db
    assert agent_cls.call_args.kwargs["persistence"] is owner.db
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


def _session_db_executor(timeouts: list, *, instant_timeout: bool = True):
    """Wrap ``ThreadPoolExecutor`` so SessionDB's ``result(timeout=...)`` is observable.

    ``run_job`` is the only caller that passes a timeout to ``Future.result`` on
    this path (``submit(SessionDB).result(timeout=...)``). Other pools used by
    ``tick`` / the agent inactivity watchdog call ``result()`` with no timeout
    and are left alone. When ``instant_timeout`` is True, the timed wait raises
    immediately instead of sleeping — the production hang path without a clock.
    """

    def factory(max_workers=1, *args, **kwargs):
        real = _REAL_TPE(max_workers=max_workers)
        orig_submit = real.submit

        def submit(fn, *a, **k):
            fut = orig_submit(fn, *a, **k)
            orig_result = fut.result

            def result(*ra, **rk):
                timeout = ra[0] if ra else rk.get("timeout")
                if timeout is not None:
                    timeouts.append(timeout)
                    if instant_timeout:
                        raise concurrent.futures.TimeoutError()
                return orig_result(*ra, **rk)

            fut.result = result
            return fut

        real.submit = submit
        return real

    return factory


class TestSessionDbInitTimeout:
    def test_sessiondb_init_preserves_multiplex_profile_context(
        self, tmp_path, monkeypatch
    ):
        """The timeout worker must construct SessionDB under the active profile."""
        from hermes_constants import (
            get_hermes_home,
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        default_home = tmp_path / "default"
        profile_home = tmp_path / "profiles" / "jobsearch"
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        observed_homes = []
        fake_db = MagicMock()

        def make_session_db(*args, **kwargs):
            observed_homes.append(get_hermes_home())
            return fake_db

        job = {"id": "profile-sessiondb", "name": "test", "prompt": "hello"}
        profile_token = set_hermes_home_override(profile_home)
        try:
            with patch("cron.scheduler._hermes_home", None), \
                 patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state_registry.acquire", side_effect=make_session_db), \
                 patch(
                     "hermes_cli.runtime_provider.resolve_runtime_provider",
                     return_value=_RUNTIME,
                 ), \
                 patch("run_agent.AIAgent") as mock_agent_cls:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent_cls.return_value = mock_agent

                success, _output, final_response, error = _run_owned_job(job, profile_home, fake_db)
        finally:
            reset_hermes_home_override(profile_token)

        assert success is True
        assert error is None
        assert final_response == "ok"
        assert observed_homes == [profile_home]

    def test_run_job_does_not_hang_when_sessiondb_init_wedges(self, tmp_path, monkeypatch):
        """run_job fails before agent construction after 3 bounded init attempts."""
        monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0.2")
        job = {"id": "wedged-sessiondb", "name": "test", "prompt": "hello"}
        timeouts: list = []

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire") as acquire_store, \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value=_RUNTIME,
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls, \
             patch(
                 "cron.scheduler.concurrent.futures.ThreadPoolExecutor",
                 side_effect=_session_db_executor(timeouts),
             ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, _output, final_response, error = _run_owned_job(job, tmp_path, acquire_store.return_value)

        # Env-resolved bound was passed to Future.result — not the 10s default,
        # and not an unbounded call.
        assert timeouts == [0.2] * 3
        assert success is False
        assert final_response == ""
        assert "session store unavailable; run not started" in error
        mock_agent_cls.assert_not_called()


    def test_timeout_resolved_from_config_yaml(self, tmp_path, monkeypatch):
        """cron.session_db_timeout_seconds in config.yaml is respected when
        the env var is not set — the canonical config-first resolution path."""
        import hermes_yaml as yaml

        monkeypatch.delenv("HERMES_CRON_SESSION_DB_TIMEOUT", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"cron": {"session_db_timeout_seconds": 0.2}})
        )
        job = {"id": "config-timeout", "name": "test", "prompt": "hello"}
        timeouts: list = []

<<<<<<< HEAD
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire") as acquire_store, \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value=_RUNTIME,
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls, \
             patch(
                 "cron.scheduler.concurrent.futures.ThreadPoolExecutor",
                 side_effect=_session_db_executor(timeouts),
             ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
||||||| dff103e05878
    def acquire(_path):
        entered.set()
        release.wait(10)
        return owner.db
=======
    def acquire(_scope, *, eager=False):
        entered.set()
        release.wait(10)
        return owner.db
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

<<<<<<< HEAD
            success, _output, _final_response, error = _run_owned_job(job, tmp_path, acquire_store.return_value)
        assert timeouts == [0.2] * 3
        assert success is False
        assert "session store unavailable; run not started" in error
        mock_agent_cls.assert_not_called()
||||||| dff103e05878
    try:
        with patch("hermes_state_registry.acquire", side_effect=acquire):
            started = time.monotonic()
            success, _, response, error = scheduler.run_job(job, execution_id=execution_id)
            elapsed = time.monotonic() - started
            assert entered.is_set()
            assert elapsed < 5
            assert not success and response == "" and error and "TimeoutError" in error
            agent_cls.assert_not_called()
            owner.db.close.assert_not_called()
            release.set()
            assert closed.wait(5), "late borrowed reference was leaked"
            owner.db.close.assert_called_once()
    finally:
        release.set()
        assert closed.wait(5)


@pytest.mark.parametrize("failure", ["wrong-store", "acquire-error"])
def test_owner_store_failure_never_runs_unpersisted_agent(owner_run, failure):
    owner, job, execution_id, agent_cls = owner_run
    wrong_db = MagicMock()
    with patch("hermes_state_registry.acquire", return_value=wrong_db,
               side_effect=OSError("store unavailable") if failure == "acquire-error" else None):
        success, _, response, error = scheduler.run_job(job, execution_id=execution_id)
    assert not success and response == ""
    assert error and ("canonical owner store" if failure == "wrong-store" else "store unavailable") in error
    agent_cls.assert_not_called()
    owner.db.close.assert_not_called()
    assert wrong_db.close.call_count == (1 if failure == "wrong-store" else 0)
=======
    try:
        with patch("persistence_assembly.open_persistence", side_effect=acquire):
            started = time.monotonic()
            success, _, response, error = scheduler.run_job(job, execution_id=execution_id)
            elapsed = time.monotonic() - started
            assert entered.is_set()
            assert elapsed < 5
            assert not success and response == "" and error and "TimeoutError" in error
            agent_cls.assert_not_called()
            owner.db.close.assert_not_called()
            release.set()
            assert closed.wait(5), "late borrowed reference was leaked"
            owner.db.close.assert_called_once()
    finally:
        release.set()
        assert closed.wait(5)


@pytest.mark.parametrize("failure", ["closed-owner", "other-generation", "acquire-error"])
def test_owner_store_failure_never_runs_unpersisted_agent(owner_run, failure):
    owner, job, execution_id, agent_cls = owner_run
    # A handle opened after the owner's store closed, or after its file was replaced, holds a store of its own.
    wrong_db = MagicMock()
    if failure == "closed-owner":
        owner.persistence.serves_same_store.side_effect = RuntimeError("persistence handle used after close")
    elif failure == "other-generation":
        owner.persistence.serves_same_store.return_value = False
    with patch("persistence_assembly.open_persistence", return_value=wrong_db,
               side_effect=OSError("store unavailable") if failure == "acquire-error" else None):
        success, _, response, error = scheduler.run_job(job, execution_id=execution_id)
    assert not success and response == ""
    assert error and ("store unavailable" if failure == "acquire-error" else "canonical owner store") in error
    agent_cls.assert_not_called()
    owner.persistence.close.assert_not_called()
    assert wrong_db.close.call_count == (0 if failure == "acquire-error" else 1)


@pytest.mark.parametrize("replaced", [False, True])
def test_a_store_replaced_under_the_owner_refuses_the_run(owner_run, tmp_path, monkeypatch, replaced):
    """A handle opened after state.db was replaced serves another generation, where the run's writes would miss
    the owner's accepted-work record, so cron refuses it; an unreplaced store serves the run."""
    import os

    from persistence.sqlite._holder import StoreHolder
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_holder import raw_holder
    owner, job, _execution_id, _agent_cls = owner_run
    monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0")
    with open_persistence(profile_scope(home=tmp_path), eager=True) as persistence:
        owner.persistence = persistence
        persistence.state_meta.set("who", "original")
        if replaced:
            donor = StoreHolder(tmp_path / "donor" / "state.db")
            donor.set_meta("who", "replacement")
            donor.close()
            held = raw_holder(persistence)
            with held._lock:
                held._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.replace(tmp_path / "donor" / "state.db", tmp_path / "state.db")
            with pytest.raises(RuntimeError, match="canonical owner store"):
                scheduler._open_cron_session_db(job)
        else:
            with scheduler._open_cron_session_db(job) as store:
                assert store.state_meta.get("who") == "original"
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


class TestDispatchGuardReleasedAfterHang:
    """End-to-end: the real bug symptom was every later tick silently
    skipping the job forever. Confirm the fix actually clears that path."""

    def test_guard_is_released_and_job_refires_after_sessiondb_hang(self, tmp_path, monkeypatch):
        import cron.scheduler as sched

<<<<<<< HEAD
        monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0.2")
        sched._parallel_pools.clear()
        sched._parallel_pool_max_workers.clear()
        sched._running_job_ids.clear()
||||||| dff103e05878
    def acquire(_path):
        calls.append(True)
        if len(calls) == 1:
            release.wait(10)
        return owner.db
=======
    def acquire(_scope, *, eager=False):
        calls.append(True)
        if len(calls) == 1:
            release.wait(10)
        return owner.db
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

        job = {
            "id": "guard-sessiondb-hang",
            "name": "guard-sessiondb-hang",
            "prompt": "hello",
            "schedule": {"kind": "interval", "minutes": 5},
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }
        timeouts: list = []
        owner_db = MagicMock()

        def admitted(fired, **kwargs):
            return _run_owned_job(fired, tmp_path, owner_db, **kwargs)

        try:
<<<<<<< HEAD
            with patch("cron.scheduler._hermes_home", tmp_path), \
                 patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state_registry.acquire", return_value=owner_db), \
||||||| dff103e05878
            return scheduler.run_job(fired, **kwargs)
        finally:
            session_cron._execution.reset(token)

    with jobs.use_cron_store(owner.db.db_path.parent):
        saved = jobs.create_job(prompt="hello", schedule="every 5m", deliver="local")
        try:
            with patch("hermes_state_registry.acquire", side_effect=acquire), \
=======
            return scheduler.run_job(fired, **kwargs)
        finally:
            session_cron._execution.reset(token)

    with jobs.use_cron_store(owner.persistence.scope.profile_dir):
        saved = jobs.create_job(prompt="hello", schedule="every 5m", deliver="local")
        try:
            with patch("persistence_assembly.open_persistence", side_effect=acquire), \
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
                 patch("cron.scheduler_authority.run_canonical_job", side_effect=admitted), \
                 patch("cron.scheduler_authority.reconcile_pending"), \
                 patch(
                     "hermes_cli.runtime_provider.resolve_runtime_provider",
                     return_value=_RUNTIME,
                 ), \
                 patch("run_agent.AIAgent") as mock_agent_cls, \
                 patch(
                     "cron.scheduler.concurrent.futures.ThreadPoolExecutor",
                     side_effect=_session_db_executor(timeouts),
                 ), \
                 patch.object(sched, "get_due_jobs", return_value=[job]), \
                 patch.object(sched, "claim_job_for_fire", return_value=True), \
                 patch.object(sched, "save_job_output", return_value="/tmp/out"), \
                 patch.object(sched, "mark_job_run"), \
                 patch.object(sched, "_deliver_result", return_value=None):
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent_cls.return_value = mock_agent

                n = sched.tick(verbose=False)  # sync=True by default: waits for the job
                assert n == 1
                assert timeouts == [0.2] * 3

                # Without the fix this would still contain the job ID forever.
                assert "guard-sessiondb-hang" not in sched.get_running_job_ids()

                # A second tick can dispatch the same job again — before the
                # fix this would log "already running — skipping" and
                # return 0.
                n2 = sched.tick(verbose=False)
                assert n2 == 1
        finally:
            sched._running_job_ids.discard(sched._inflight_key("guard-sessiondb-hang"))
            sched._shutdown_parallel_pool()


# ===========================================================================
# Bug #72782: late SessionDB result leaks FDs after timeout abandonment
# ===========================================================================



class TestLateSessionDbClosedAfterTimeout:
    """End-to-end: when SessionDB init times out but later completes inside the
    abandoned worker, the orphaned result must be closed (#72782)."""

    def test_late_session_db_result_is_closed(self, tmp_path, monkeypatch):
        """Each timed-out borrow is returned, without closing the owner's live store."""
        import hermes_state_registry as registry

        monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0.02")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        release = threading.Event()
        late_db_holder = []
        owner_db = registry.acquire(tmp_path / "state.db")
        real_acquire = registry.acquire
        refs_before = registry.stats()["total_refcounts"]

        def hanging_borrow(path):
            assert release.wait(timeout=10)
            db = real_acquire(path)
            late_db_holder.append(db)
            return db

        job = {"id": "late-close-test", "name": "test", "prompt": "hello"}
        try:
            with patch("cron.scheduler._hermes_home", tmp_path), \
                 patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state_registry.acquire", side_effect=hanging_borrow), \
                 patch("hermes_state_registry.release_or_close", wraps=registry.release_or_close) as returned, \
                 patch("run_agent.AIAgent") as agent_cls:
                success, _, response, error = _run_owned_job(job, tmp_path, owner_db)
                assert not success and response == ""
                assert "session store unavailable; run not started" in error
                agent_cls.assert_not_called()
                assert late_db_holder == []
                release.set()
                from tests.waiting import wait_until
                wait_until(
                    lambda: returned.call_count == 3 and registry.stats()["total_refcounts"] == refs_before,
                    timeout=5, what="timed-out store borrows to return",
                )
                assert len(late_db_holder) == 3
                assert all(db is owner_db for db in late_db_holder)
                assert returned.call_count == 3
                assert all(call.args == (owner_db,) for call in returned.call_args_list)
                assert registry.stats()["total_refcounts"] == refs_before
                owner_db.create_session("owner-still-open", source="cron")
                assert owner_db.get_session("owner-still-open") is not None
        finally:
            release.set()
            registry.release(owner_db)


<<<<<<< HEAD
# ===========================================================================
# Gated runs must release the store acquired before their acknowledgement
# ===========================================================================

class TestSessionDbEarlyReturnCleanup:
    """Acquisition before gates is safe because every early return releases its handle."""

    def test_wake_gate_false_releases_session_db(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_SESSION_DB_TIMEOUT", raising=False)
        job = {
            "id": "gated-no-db",
            "name": "gated-no-db",
            "prompt": "hello",
            "script": "gate.py",
        }

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire") as mock_db_cls, \
             patch(
                 "cron.scheduler._run_job_script_with_claim_heartbeat",
                 return_value=(True, '{"wakeAgent": false}'),
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            success, _output, _final_response, _error = _run_owned_job(job, tmp_path, mock_db_cls.return_value)

        assert success is True
        mock_db_cls.assert_called_once()
        mock_db_cls.return_value.close.assert_called_once()
        mock_agent_cls.assert_not_called()



@pytest.mark.parametrize("wrong_store", [False, True], ids=["unavailable", "noncanonical"])
def test_owner_store_failure_never_runs_unpersisted_agent(tmp_path, monkeypatch, wrong_store):
    """An acquisition failure stops before constructing an unpersisted agent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner_db = MagicMock()
    other_db = MagicMock()
    job = {"id": "owner-db", "name": "test", "prompt": "hello"}
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=other_db,
               side_effect=None if wrong_store else OSError("store unavailable")) as acquire_store, \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value=_RUNTIME,
         ), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        success, _output, response, error = _run_owned_job(job, tmp_path, owner_db)

    assert success is False
    assert response == ""
    assert error and "store unavailable" in error
    mock_agent_cls.assert_not_called()
    assert acquire_store.call_count == 3
    owner_db.close.assert_not_called()
    assert other_db.close.call_count == (3 if wrong_store else 0)
||||||| dff103e05878
def test_wake_gate_false_never_opens_session_db(owner_run):
    _, job, execution_id, agent_cls = owner_run
    job["script"] = "gate.py"
    with patch("hermes_state_registry.acquire") as acquire, \
         patch("cron.scheduler._run_job_script_with_claim_heartbeat", return_value=(True, '{"wakeAgent": false}')):
        success, _, _, error = scheduler.run_job(job, execution_id=execution_id)
    assert success and error is None
    acquire.assert_not_called()
    agent_cls.assert_not_called()
=======
def test_wake_gate_false_never_opens_session_db(owner_run):
    _, job, execution_id, agent_cls = owner_run
    job["script"] = "gate.py"
    with patch("persistence_assembly.open_persistence") as acquire, \
         patch("cron.scheduler._run_job_script_with_claim_heartbeat", return_value=(True, '{"wakeAgent": false}')):
        success, _, _, error = scheduler.run_job(job, execution_id=execution_id)
    assert success and error is None
    acquire.assert_not_called()
    agent_cls.assert_not_called()
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
