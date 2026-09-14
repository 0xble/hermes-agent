"""Cross-process SQLite allocation and exact-result release invariants."""
import json
import multiprocessing
import sqlite3
import time

import pytest

from tools import async_delegation as ad


def _operation(kind, parent, ref):
    if kind == "reserve":
        return ad.reserve_delegation_metadata(parent_task_id=parent, owner={"session_id": "owner"},
                                               task_labels=["Task"])["thread_refs"]
    return ad.release_result_retention(owner={"session_id": "owner"}, parent_task_id=parent,
                                       attempts={ref: 0})


def _contender(path, kind, parent, pipe):
    # Schema is initialized by the test before either process enters the race.
    ad._connect = lambda: sqlite3.connect(path, timeout=0)
    pipe.send("ready")
    while pipe.recv() == "run":
        try:
            pipe.send(("ok", _operation(kind, parent, "B")))
        except sqlite3.OperationalError as exc:
            pipe.send(("locked", str(exc)))
    pipe.close()


@pytest.mark.parametrize("kind", ["reserve", "release"])
def test_metadata_read_modify_write_excludes_another_process(tmp_path, monkeypatch, kind):
    path = tmp_path / "async.db"
    monkeypatch.setattr(ad, "_db_path", lambda: path)
    owner = {"session_id": "owner"}
    allocation = ad.reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=["A", "B"])
    parent = allocation["parent_task_id"]
    if kind == "release":
        metadata = {**allocation, "threads": [{"thread_ref": ref, "task_index": i}
                                              for i, ref in enumerate(["A", "B"])],
                    "attempts": {"A": 0, "B": 0}}
        ad._persist_dispatch({"delegation_id": "two-results", "dispatched_at": time.time(),
                              "delegation_metadata": metadata})
        ad._persist_completion({"delegation_id": "two-results", "status": "completed"},
                               {"results": [{"task_index": 0}, {"task_index": 1}]})

    ctx = multiprocessing.get_context("spawn")
    local, remote = ctx.Pipe()
    process = ctx.Process(target=_contender, args=(str(path), kind, parent, remote))
    process.start()
    remote.close()
    responses = []
    target = "SELECT next_thread_number" if kind == "reserve" else "SELECT delegation_id, task_json, result_json"

    def receive():
        assert local.poll(15), "SQLite contender did not respond"
        return local.recv()

    def after_read():
        if not responses:
            local.send("run")
            responses.append(receive())

    class Cursor(sqlite3.Cursor):
        def fetchone(self):
            row = super().fetchone()
            if self.watched:
                after_read()
            return row

        def fetchall(self):
            rows = super().fetchall()
            if self.watched:
                after_read()
            return rows

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = self.cursor(factory=Cursor)
            cursor.watched = sql.startswith(target)
            return cursor.execute(sql, parameters)

    try:
        assert receive() == "ready"
        monkeypatch.setattr(ad, "_connect", lambda: sqlite3.connect(path, factory=Connection))
        first = _operation(kind, parent, "A")
        # The process-local Python lock cannot establish this exclusion.
        assert responses == [("locked", "database is locked")]
        local.send("run")
        status, second = receive()
        assert status == "ok"  # Transaction released, retry makes progress.
        if kind == "reserve":
            assert first != second
            assert first == ["C"] and second == ["D"]
        else:
            assert first == second == 1
            with sqlite3.connect(path) as conn:
                task, result = conn.execute("SELECT task_json,result_json FROM async_delegations WHERE delegation_id='two-results'").fetchone()
            assert json.loads(task)["result_released"] == ["A:0", "B:0"]
            assert ad._retained_result_keys(json.loads(task), json.loads(result)) == set()
    finally:
        if process.is_alive():
            local.send("stop")
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
        local.close()
    assert process.exitcode == 0
