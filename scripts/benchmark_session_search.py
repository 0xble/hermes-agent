#!/usr/bin/env python3
"""Compare serial recall with a concurrent SQLite writer and many search threads.

Uses a disposable store only. Run from a PM-activated source checkout:
    python scripts/benchmark_session_search.py --messages 10000 --workers 64
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def summarize(values):
    ordered = sorted(values)
    return {"median_ms": round(statistics.median(values), 3),
            "p95_ms": round(ordered[int(.95 * (len(ordered) - 1))], 3),
            "max_ms": round(max(values), 3), "completed": len(values)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=10000)
    parser.add_argument("--workers", type=int, default=64)
    args = parser.parse_args()
    if args.messages < 300 or not 1 <= args.workers <= 64:
        parser.error("requires >=300 messages and 1..64 workers")
    with tempfile.TemporaryDirectory(prefix="search-load-", dir=os.environ.get("TMPDIR")) as home:
        os.environ["HERMES_HOME"] = home
        from hermes_state import SessionDB
        from tools.session_search_tool import session_search

        db = SessionDB(Path(home) / "state.db")
        try:
            db.create_session("history", source="cli", model="benchmark")
            conn = db._conn
            assert conn is not None
            with db._lock:
                conn.executemany(
                    "INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, ?, ?, ?)",
                    [("history", "user", f"needle shared token {i % 100}", i) for i in range(args.messages)])
                conn.commit()
            gate = threading.Barrier(args.workers)

            def recall(concurrent=False):
                if concurrent:
                    gate.wait(timeout=30)
                started = time.monotonic()
                rows = db.search_messages("needle", limit=300, fields=["id"])
                assert len(rows) == 300
                return (time.monotonic() - started) * 1000

            serial = [recall() for _ in range(args.workers)]
            writing, done = threading.Event(), threading.Event()
            writer_iterations = []

            def writer():
                with db._lock:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE sessions SET title='uncommitted' WHERE id='history'")
                    conn.set_progress_handler(lambda: writing.set() or 0, 1000)
                    try:
                        started = time.monotonic()
                        while not done.is_set() and time.monotonic() - started < 30:
                            conn.execute("""WITH RECURSIVE n(x) AS
                                (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000)
                                SELECT sum(a.x*b.x) FROM n a, n b""").fetchone()
                            writer_iterations.append(1)
                        assert done.is_set(), "writer workload exceeded benchmark bound"
                    finally:
                        conn.set_progress_handler(None, 0)
                        conn.rollback()

            with ThreadPoolExecutor(max_workers=args.workers + 1) as executor:
                writer_future = executor.submit(writer)
                try:
                    assert writing.wait(5)
                    started = time.monotonic()
                    futures = [executor.submit(recall, True) for _ in range(args.workers)]
                    loaded = [future.result(timeout=30) for future in futures]
                    # The full tool must hydrate metadata/windows under the same held
                    # writer lock, not just finish the MATCH on a pooled reader.
                    tool_started = time.monotonic()
                    payload = json.loads(session_search(query="needle", db=db, detail="full"))
                    assert payload["success"] and payload["count"] == 1
                    assert payload["results"][0]["messages"]
                    tool_ms = (time.monotonic() - tool_started) * 1000
                    total_ms = (time.monotonic() - started) * 1000
                finally:
                    done.set()
                writer_future.result(timeout=30)
            plans = {}
            for sort, order in (("rank", "ORDER BY rank"),
                                ("newest", "ORDER BY rank, m.timestamp DESC, m.id DESC")):
                sql, params = db._fts_match_sql("messages_fts", "needle", order, limit=300, offset=0,
                                                include_inactive=False, source_filter=None,
                                                exclude_sources=None, role_filter=None)
                plans[sort] = [row[3] for row in db._read_all("EXPLAIN QUERY PLAN " + sql, params)]
            print(json.dumps({"messages": args.messages, "workers": args.workers,
                              "serial": summarize(serial), "busy_writer": summarize(loaded),
                              "loaded_batch_and_tool_ms": round(total_ms, 3),
                              "full_tool_under_writer_ms": round(tool_ms, 3),
                              "writer_sql_iterations": len(writer_iterations), "plans": plans}, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    main()
