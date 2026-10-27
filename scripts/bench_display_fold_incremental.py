#!/usr/bin/env python3
"""Synthetic fixed-affected-set comparison against an explicit full-index baseline.

Run with scripts/run-in-hermes-env under heavy-slot. All databases are disposable,
created under TMPDIR. Never opens a profile database. Component timing follows the
original 21-pair warm SAVEPOINT protocol, with a real freshness selection. The
archive arm additionally runs archive_and_compact with original FTS triggers.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hermes_state import SessionDB
from hermes_state_common import SCHEMA_SQL, DEFERRED_INDEX_SQL
from hermes_state_messages import SessionMessagesMixin
from hermes_state_write_metrics import WriteMetrics, current_write_metrics


def percentile(samples, quantile):
    return sorted(samples)[int((len(samples) - 1) * quantile)]


def summary(samples):
    return {"median_ms": statistics.median(samples), "p95_ms": percentile(samples, .95), "samples_ms": samples}


def load_baseline(base, directory):
    source = subprocess.check_output(["git", "show", f"{base}:hermes_state_messages.py"], cwd=ROOT, text=True, encoding="utf-8", timeout=30)
    path = directory / "baseline.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("display_fold_baseline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SessionMessagesMixin


def history_rows(db, count, group_size=3, nulls=0):
    for index in range(count):
        logical = index // group_size if index < 20 * group_size else index
        content, timestamp = f"history {logical}:" + "x" * 256, float(logical)
        identity = db._display_identity(("assistant", content, timestamp, None, None, None))
        order = logical * group_size + 1 if index < 20 * group_size else index + 1
        yield ("s", "assistant", content, timestamp, None if index >= count - nulls else identity, order)


def incoming():
    return [{"role": "assistant", "content": f"history {i % 20}:" + "x" * 256, "timestamp": float(i % 20)}
            for i in range(40)]


def state(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT id, active, compacted, display_identity, display_order FROM messages ORDER BY id")]


class ComponentDB(SessionMessagesMixin):
    def __init__(self, path):
        self.db_path = path
        self._lock = threading.Lock()
        self._wal_active, self.read_only = True, False
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-2048")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.executescript(DEFERRED_INDEX_SQL)
        self.reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        self.reader.row_factory = sqlite3.Row

    @contextmanager
    def _read_ctx(self):
        yield self.reader

    def close(self):
        self.reader.close()
        self._conn.close()


def component(path, count, rounds, baseline):
    db = ComponentDB(path)
    conn = db._conn
    try:
        conn.execute("INSERT INTO sessions(id, source, started_at) VALUES ('s', 'synthetic', 0)")
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp, display_identity, display_order, active, compacted) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 1)", history_rows(db, count))
        conn.commit()
        start = time.perf_counter_ns()
        plan = db._display_fold_plan("s")
        plan_ms = (time.perf_counter_ns() - start) / 1e6
        assert plan is not None
        conn.execute("BEGIN IMMEDIATE")
        selection = db._begin_display_fold(conn, plan)
        assert type(selection).__name__ == "_AffectedFold"
        for index, msg in enumerate(incoming()):
            identity = db._display_identity(("assistant", msg["content"], msg["timestamp"], None, None, None))
            conn.execute(
                "INSERT INTO messages(session_id, role, content, timestamp, display_identity, display_order) "
                "VALUES ('s', 'assistant', ?, ?, ?, ?)",
                (msg["content"], msg["timestamp"], identity, count + index + 1))

        def full_index():
            visible = baseline._visible_display_index(db, conn, "s")
            baseline._fold_display_orders(db, conn, visible, plan)

        arms = {"full_index": full_index, "incremental": lambda: db._reconcile_display_orders(conn, "s", selection)}
        folded = {}
        counts = {}
        vm_steps = {}
        for name, fn in arms.items():
            conn.execute("SAVEPOINT verify")
            metrics = WriteMetrics()
            token = current_write_metrics.set(metrics)
            ticks = [0]
            conn.set_progress_handler(lambda: ticks.__setitem__(0, ticks[0] + 100) or 0, 100)
            try:
                fn()
            finally:
                conn.set_progress_handler(None, 0)
                current_write_metrics.reset(token)
            counts[name], vm_steps[name] = metrics.rows, ticks[0]
            folded[name] = state(conn)
            conn.execute("ROLLBACK TO verify")
            conn.execute("RELEASE verify")
        assert folded["full_index"] == folded["incremental"]
        conn.execute("SAVEPOINT oracle")
        db._reconcile_display_orders(conn, "s")
        assert state(conn) == folded["incremental"]
        conn.execute("ROLLBACK TO oracle")
        conn.execute("RELEASE oracle")
        for fn in arms.values():
            conn.execute("SAVEPOINT warmup")
            fn()
            conn.execute("ROLLBACK TO warmup")
            conn.execute("RELEASE warmup")
        samples = {name: [] for name in arms}
        for iteration in range(rounds):
            for name in (list(arms) if iteration % 2 == 0 else list(reversed(arms))):
                conn.execute("SAVEPOINT timing")
                metrics = WriteMetrics()
                token = current_write_metrics.set(metrics)
                start = time.perf_counter_ns()
                try:
                    arms[name]()
                finally:
                    elapsed = (time.perf_counter_ns() - start) / 1e6
                    current_write_metrics.reset(token)
                samples[name].append(elapsed)
                conn.execute("ROLLBACK TO timing")
                conn.execute("RELEASE timing")
        query_plan = [tuple(row) for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id, display_identity, display_order FROM messages "
            "INDEXED BY idx_messages_display_identity WHERE session_id = ? AND display_identity IN (?) "
            "AND (active = 1 OR compacted = 1)", ("s", plan[1][1]))]
        return {"history_rows": count, "affected_rows": 100, "planning_ms": plan_ms, "equal": True,
                "rows": counts, "approx_vm_steps": vm_steps, "query_plan": query_plan,
                **{name: summary(values) for name, values in samples.items()}}
    finally:
        if conn.in_transaction:
            conn.rollback()
        db.close()


def archive(path, count, rounds, baseline, named):
    db = SessionDB(path)
    try:
        db.create_session("s", source="synthetic")
        db._execute_write(lambda conn: conn.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp, display_identity, display_order, active, compacted) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 1)", history_rows(db, count)))
        db.append_messages_batch("s", incoming())
        live = db.get_messages_as_conversation("s", include_row_ids=True)
        assert len(live) == 40
        original_state = state(db._conn)
        anchor = original_state[-1][0]
        methods = ("_display_fold_plan", "_visible_display_index", "_fold_display_orders", "_reconcile_display_orders")
        original = {name: getattr(db, name) for name in methods}
        begin = db._begin_display_fold
        results = {name: [] for name in ("full_index", "incremental")}
        after = {}
        for iteration in range(rounds):
            for name in (list(results) if iteration % 2 == 0 else list(reversed(results))):
                for method in methods:
                    setattr(db, method, (getattr(baseline, method).__get__(db) if name == "full_index" else original[method]))
                db._begin_display_fold = (lambda conn, plan: plan) if name == "full_index" else begin
                reconcile = db._reconcile_display_orders
                seen = []

                def measured(conn, session_id, plan=None):
                    seen.append(current_write_metrics.get())
                    return reconcile(conn, session_id, plan)

                db._reconcile_display_orders = measured
                start = time.perf_counter_ns()
                db.archive_and_compact("s", copy.deepcopy(incoming()),
                    watermark=anchor, covered_ids=[m["_row_id"] for m in live] if named else None)
                elapsed = (time.perf_counter_ns() - start) / 1e6
                metrics = seen[-1]
                results[name].append({"api_ms": elapsed, "hold_ms": 1000 * (
                    metrics.phases["callback_total"] + metrics.phases["commit"]),
                    "phases": metrics.phases, "rows": metrics.rows})
                after[name] = state(db._conn)
                db._reconcile_display_orders = original["_reconcile_display_orders"]
                db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))
                assert state(db._conn) == after[name]

                def restore(conn):
                    conn.execute("DELETE FROM messages WHERE id > ?", (anchor,))
                    conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'messages'", (anchor,))
                    conn.executemany("UPDATE messages SET active=?, compacted=?, display_identity=?, display_order=? WHERE id=?",
                                     [(active, compacted, identity, order, row_id)
                                      for row_id, active, compacted, identity, order in original_state[count:]])
                db._execute_write(restore)
                assert state(db._conn) == original_state
        assert after["full_index"] == after["incremental"]
        return {"history_rows": count, "named": named, "equal": True, "samples": results,
                "hold": {name: summary([sample["hold_ms"] for sample in values]) for name, values in results.items()}}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Explicit pre-incremental baseline Git revision")
    parser.add_argument("--rows", default="30000,60000,100000")
    parser.add_argument("--rounds", type=int, default=21)
    parser.add_argument("--archive-rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.getLogger("hermes_state").setLevel(logging.ERROR)
    result = {"base": args.base, "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, encoding="utf-8", cwd=ROOT, timeout=30).strip(),
              "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True, encoding="utf-8", cwd=ROOT, timeout=30)),
              "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                  "hermes_state_display_fold.py", "hermes_state_messages.py", "hermes_state_message_archive.py",
                  "tests/hermes_state/test_display_fold_incremental.py", "scripts/bench_display_fold_incremental.py")},
              "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "rounds": args.rounds, "archive_rounds": args.archive_rounds,
              "component": [], "archive": []}
    with tempfile.TemporaryDirectory(prefix="display-fold-implementation-", dir=os.environ["TMPDIR"]) as temporary:
        directory = Path(temporary)
        baseline = load_baseline(args.base, directory)
        for size in map(int, args.rows.split(",")):
            component_result = component(directory / f"component-{size}.sqlite", size, args.rounds, baseline)
            result["component"].append(component_result)
            print("COMPONENT " + json.dumps(component_result), flush=True)
            for named in (False, True):
                archive_result = archive(directory / f"archive-{size}-{named}.sqlite", size, args.archive_rounds, baseline, named)
                result["archive"].append(archive_result)
                print("ARCHIVE " + json.dumps(archive_result), flush=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
