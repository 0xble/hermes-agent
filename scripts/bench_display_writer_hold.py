#!/usr/bin/env python3
"""Compare real archive writer holds with one CPU-bound Python thread.

Use the repository interpreter under heavy-slot. Only disposable TMPDIR SQLite
files are opened. A pinned Git baseline supplies the original fold methods, not a
reimplementation. Reports nearest-rank p99 with every sample and semantic checks.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import logging
import math
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
from hermes_state_display_fold import DisplayFoldMixin
from hermes_state_write_metrics import current_write_metrics


def load_baseline(base, directory):
    source = subprocess.check_output(["git", "show", f"{base}:hermes_state_display_fold.py"], cwd=ROOT)
    path = directory / "baseline_fold.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("baseline_fold", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.DisplayFoldMixin


def cpu_load(stop, ready):
    ready.set()
    n = 1
    while not stop.is_set():
        n = (n * 1664525 + 1013904223) & 0xFFFFFFFF


def state(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT id, active, compacted, display_identity, display_order FROM messages ORDER BY id")]


def summarize(samples):
    values = sorted(sample["hold_ms"] for sample in samples)
    return {"samples": len(values), "p50_ms": statistics.median(values),
            "p99_ms": values[math.ceil(len(values) * .99) - 1], "max_ms": max(values)}


def run_case(path, case, args, baseline):
    db = SessionDB(path)
    try:
        db.create_session("s", source="synthetic")
        db.append_messages_batch("s", [
            {"role": "assistant", "content": f"history {i}:" + "x" * args.payload_bytes,
             "timestamp": 1.7e9 + i} for i in range(args.rows)])
        # Retain compacted history and a small active tail, as in the production incident.
        db._write_sql("UPDATE messages SET active=0, compacted=1 WHERE id <= ?", (args.rows - 20,))
        conn = db._conn
        initial = state(conn)
        tail = db.get_messages_as_conversation("s", include_row_ids=True)
        messages = [{"role": "assistant", "content": "synthetic compacted summary", "timestamp": 1.8e9,
                     "_compressed_summary": True},
                    *tail[-2:]]
        variants = {"before": baseline, "after": DisplayFoldMixin}
        names = [name for name, fn in inspect.getmembers(DisplayFoldMixin, inspect.isfunction)]
        samples = {name: [] for name in variants}
        digest = None
        for iteration in range(-1, args.rounds):
            for name in (list(variants) if iteration % 2 == 0 else list(reversed(variants))):
                cls = variants[name]
                for method in names:
                    setattr(db, method, getattr(cls, method).__get__(db))
                if case == "no_plan":
                    db._display_fold_plan = lambda session_id: None
                if case == "null_backlog":
                    db._write_sql("UPDATE messages SET display_identity=NULL, display_order=NULL WHERE session_id='s'")
                original = db._reconcile_display_orders
                seen = []

                def measured(conn, sid, plan=None):
                    seen.append(current_write_metrics.get())
                    return original(conn, sid, plan)

                db._reconcile_display_orders = measured
                stop, ready = threading.Event(), threading.Event()
                load = threading.Thread(target=cpu_load, args=(stop, ready))
                load.start()
                ready.wait()
                start = time.perf_counter()
                try:
                    db.archive_and_compact("s", messages, watermark=args.rows, tail_count=2)
                    api_ms = 1000 * (time.perf_counter() - start)
                finally:
                    stop.set()
                    load.join()
                metrics = seen[-1]
                sample = {"hold_ms": 1000 * (metrics.phases["callback_total"] + metrics.phases["commit"]),
                          "api_ms": api_ms, "phases": metrics.phases, "rows": metrics.rows}
                if iteration >= 0:
                    samples[name].append(sample)
                result = state(conn)
                current = hashlib.sha256(repr(result).encode()).hexdigest()
                if digest is None:
                    digest = current
                assert current == digest, (case, name, "stored fold differs")
                # Check against a complete payload fold without a cached plan, off load.
                db._reconcile_display_orders = original
                db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))
                assert state(conn) == result, "payload oracle differs"

                def restore(conn):
                    conn.execute("DELETE FROM messages WHERE id > ?", (args.rows,))
                    conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='messages'", (args.rows,))
                    conn.executemany(
                        "UPDATE messages SET active=?, compacted=?, display_identity=?, display_order=? WHERE id=?",
                        [(active, compacted, identity, order, row_id)
                         for row_id, active, compacted, identity, order in initial])
                db._execute_write(restore)
                assert state(conn) == initial
        return {"case": case, "semantic_digest": digest,
                "summary": {name: summarize(values) for name, values in samples.items()}, "samples": samples}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--rows", type=int, default=400)
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--payload-bytes", type=int, default=4096)
    parser.add_argument("--cases", default="healthy,null_backlog,no_plan")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.rows < 20 or args.rounds < 1 or args.payload_bytes < 0:
        parser.error("rows >= 20, rounds >= 1, payload-bytes >= 0 required")
    logging.getLogger("hermes_state").setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="writer-hold-", dir=os.environ["TMPDIR"]) as directory:
        directory = Path(directory)
        baseline = load_baseline(args.baseline, directory)
        cases = [run_case(directory / f"{case}.sqlite", case, args, baseline) for case in args.cases.split(",")]
    result = {"baseline_sha": subprocess.check_output(["git", "rev-parse", args.baseline], cwd=ROOT, text=True, encoding="utf-8").strip(),
              "candidate_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8").strip(),
              "candidate_source_sha256": hashlib.sha256((ROOT / "hermes_state_display_fold.py").read_bytes()).hexdigest(),
              "python": sys.version, "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
              "switch_interval_s": sys.getswitchinterval(), "cpu_threads": 1,
              "protocol": vars(args) | {"output": str(args.output)}, "cases": cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "cases": [{"case": c["case"], "summary": c["summary"]} for c in cases]}, indent=2))


if __name__ == "__main__":
    main()
