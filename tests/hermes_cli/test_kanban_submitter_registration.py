"""The owner submitter honors the orphan-worker contract from upstream #121556."""

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_client
from tests.fakes.fake_llm_provider import FakeLLMServer, Text


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "pinned-board.db"
    kbc.init_db(path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: True)
    with kbc.connect_closing(path) as conn:
        yield conn, path


def _submit(monkeypatch, path, task):
    # A subprocess's scrub must not remove the dispatcher's environment.
    with monkeypatch.context() as child:
        for key, value in {
            "BOARD": "default", "DB": path, "TASK": task.id,
            "RUN_ID": task.current_run_id, "CLAIM_LOCK": task.claim_lock,
        }.items():
            child.setenv(f"HERMES_KANBAN_{key}", str(value))
        return kanban_worker_client._submit()


@pytest.mark.parametrize("check_admission_order", [False, True], ids=["retention", "admission-order"])
def test_submitter_survives_dispatcher_exit_before_parent_records_pid(
        board, monkeypatch, tmp_path, check_admission_order):
    conn, path = board
    tid = kb.create_task(conn, title="orphan submitter", assignee="default",
                         workspace_kind="dir", workspace_path=str(tmp_path))
    with FakeLLMServer([Text("working")]) as provider:
        async def owner_run(params, board_db):
            # Admission must observe the registration on both rows, through a fresh
            # connection to the pinned DB, not merely after the submitter returns.
            assert board_db == str(path)
            if check_admission_order:
                with kbc.connect_closing(path) as owner_conn:
                    task = owner_conn.execute("SELECT worker_pid, worker_started_at FROM tasks WHERE id = ?",
                                              (tid,)).fetchone()
                    run = owner_conn.execute("SELECT worker_pid, worker_started_at FROM task_runs WHERE id = ?",
                                             (params["run_id"],)).fetchone()
                    assert task["worker_pid"] == run["worker_pid"] == os.getpid()
                    assert task["worker_started_at"] == run["worker_started_at"]
                    assert task["worker_started_at"]
            request = Request(provider.base_url + "/chat/completions", data=json.dumps({
                "model": "fake-model", "messages": [{"role": "user", "content": params["task_id"]}],
                "tools": [{"type": "function", "function": {"name": "kanban_complete"}}],
            }).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                assert response.status == 200
            return 0

        monkeypatch.setattr(kanban_worker_client, "run", owner_run)

        def spawn_then_exit(task, workspace):
            assert task.worker_pid is None
            assert _submit(monkeypatch, path, task) == 0
            # BaseException escapes the dispatcher's spawn-failure handler, just as SIGKILL
            # does, after the child starts but before _set_worker_pid can execute.
            raise SystemExit("dispatcher killed before PID record")

        with pytest.raises(SystemExit, match="dispatcher killed"):
            kbd.dispatch_once(conn, spawn_fn=spawn_then_exit, reconcile_orphans=False)
        conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tid,))
        conn.commit()
        reclaimed = kb.release_stale_claims(conn)
        assert reclaimed == 0
        result = kbd.dispatch_once(conn, spawn_fn=spawn_then_exit, reconcile_orphans=False)
        assert result.spawned == []
        assert len(provider.main_requests()) == 1
    task = kb.get_task(conn, tid)
    assert (task.status, task.worker_pid) == ("running", os.getpid())
    assert conn.execute("SELECT worker_pid FROM task_runs WHERE id = ?",
                        (task.current_run_id,)).fetchone()[0] == os.getpid()


@pytest.mark.parametrize("failure", ["refused", "exception"])
def test_failed_registration_never_connects_to_owner(board, monkeypatch, failure):
    conn, path = board
    tid = kb.create_task(conn, title="unregistered submitter", assignee="default")
    task = kb.claim_task(conn, tid)
    attempts, submissions = [], []

    def adopt(conn, task_id, run_id, pid):
        attempts.append((task_id, run_id, pid))
        if failure == "exception":
            raise RuntimeError("registration write failed")
        return False

    async def owner_run(params, board_db):
        submissions.append(params)
        return 0

    monkeypatch.setattr(kbd, "adopt_worker_pid", adopt)
    monkeypatch.setattr(kanban_worker_client, "run", owner_run)
    assert _submit(monkeypatch, path, task) != 0
    assert attempts == [(tid, task.current_run_id, os.getpid())]
    assert submissions == []
    assert kb.get_task(conn, tid).worker_pid is None


@pytest.mark.parametrize("winner", ["adoption", "reclaim"])
def test_registration_racing_reclaim_cas_has_one_winner(board, monkeypatch, winner):
    conn, path = board
    tid = kb.create_task(conn, title="racing submitter", assignee="default")
    task = kb.claim_task(conn, tid)
    conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tid,))
    conn.commit()
    adopted, submissions = [], []
    real_adopt = kbd.adopt_worker_pid

    def adopt(worker_conn, task_id, run_id, pid):
        result = real_adopt(worker_conn, task_id, run_id, pid)
        adopted.append(result)
        return result

    async def owner_run(params, board_db):
        with kbc.connect_closing(path) as owner_conn:
            assert kb.get_task(owner_conn, tid).worker_pid == os.getpid()
        submissions.append(params)
        return 0

    monkeypatch.setattr(kbd, "adopt_worker_pid", adopt)
    monkeypatch.setattr(kanban_worker_client, "run", owner_run)

    if winner == "adoption":
        real_terminate = kb._terminate_reclaimed_worker

        def register_after_stale_select(pid, claim_lock, **kwargs):
            # Reclaim has selected NULL, but has not acquired the CAS write lock.
            assert pid is None
            assert _submit(monkeypatch, path, task) == 0
            return real_terminate(pid, claim_lock, **kwargs)

        with monkeypatch.context() as sweep:
            sweep.setattr(kb, "_terminate_reclaimed_worker", register_after_stale_select)
            assert kb.release_stale_claims(conn) == 0
        # The stale snapshot's CAS lost. The next sweep sees the live PID and
        # extends the TTL rather than reclaiming it.
        assert kb.release_stale_claims(conn) == 0
        assert kb.get_task(conn, tid).claim_expires > 1
        assert adopted == [True]
        assert len(submissions) == 1
    else:
        assert kb.release_stale_claims(conn) == 1
        replacement = kb.claim_task(conn, tid)
        assert _submit(monkeypatch, path, task) != 0
        assert adopted == [False]
        assert submissions == []
        assert kb.get_task(conn, tid).current_run_id == replacement.current_run_id

    current = kb.get_task(conn, tid)
    assert current.worker_pid == (os.getpid() if winner == "adoption" else None)
    reclaimed = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'reclaimed'",
                             (tid,)).fetchone()[0]
    assert reclaimed == (winner == "reclaim")
