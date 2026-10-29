#!/usr/bin/env python3
"""Matched scratch-only archive/FTS measurements, with insertion/hash ablations."""
from __future__ import annotations

import argparse
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


def _load(base, source, name, directory):
    path = directory / (name + ".py")
    path.write_bytes(subprocess.check_output(["git", "show", f"{base}:{source}"], cwd=ROOT))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _spin(stop, ready):
    ready.set()
    value = 1
    while not stop.is_set():
        value = (value * 1664525 + 1013904223) & 0xFFFFFFFF


def _case(directory, base, rounds, loaded):
    from agent import transcript_repair
    from hermes_state import SessionDB
    from hermes_state_messages import SessionMessagesMixin
    from hermes_state_write_metrics import current_write_metrics

    old_insert = _load(base, "hermes_state_messages.py", "baseline_messages", directory).SessionMessagesMixin._insert_message_rows
    old_hash = _load(base, "agent/transcript_repair.py", "baseline_repair", directory).transcript_row_snapshot
    new_insert, new_hash = SessionMessagesMixin._insert_message_rows, transcript_repair.transcript_row_snapshot
    variants = {"before": (old_insert, old_hash), "batch_only": (new_insert, old_hash),
                "hash_only": (old_insert, new_hash), "after": (new_insert, new_hash)}
    path = directory / ("loaded.sqlite" if loaded else "idle.sqlite")
    db = SessionDB(path)
    samples = {name: [] for name in variants}
    try:
        assert db._fts_cjk_available, "matched benchmark requires the native third index"
        db.create_session("s", source="synthetic")
        db.append_messages_batch("s", [
            {"role": "assistant" if i % 2 else "user", "content": f"benchword 日本語検索 {i} " + "x" * 4096,
             "reasoning_content": "r" * 4096 if i % 2 else None, "timestamp": 1700000000 + i}
            for i in range(400)])
        db._write_sql("UPDATE messages SET active=0, compacted=1 WHERE id <= 200")
        initial = [tuple(row) for row in db._read_all("SELECT id, active, compacted, display_identity, display_order FROM messages ORDER BY id")]
        tail = db.get_messages_as_conversation("s", include_row_ids=True)
        template = [{"role": "user", "content": "benchword freshsummary 日本語検索", "timestamp": 1800000000,
                     "_compressed_summary": True, "message_uid": "a" * 32}, *tail]
        reconcile = db._reconcile_display_orders
        seen = []

        def observe(conn, sid, plan=None):
            seen.append(current_write_metrics.get())
            return reconcile(conn, sid, plan)

        db._reconcile_display_orders = observe
        expected = None
        for iteration in range(-1, rounds):
            order = list(variants) if iteration % 2 == 0 else list(reversed(variants))
            for name in order:
                insert, digest = variants[name]
                db._insert_message_rows = insert.__get__(db)
                transcript_repair.transcript_row_snapshot = digest
                messages = copy.deepcopy(template)
                stop, ready = threading.Event(), threading.Event()
                thread = threading.Thread(target=_spin, args=(stop, ready)) if loaded else None
                if thread:
                    thread.start()
                    ready.wait()
                started = time.perf_counter()
                try:
                    db.archive_and_compact("s", messages, watermark=400, tail_count=200)
                    api_ms = (time.perf_counter() - started) * 1000
                finally:
                    stop.set()
                    if thread:
                        thread.join()
                metrics = seen[-1]
                sample = {"hold_ms": 1000 * (metrics.phases["callback_total"] + metrics.phases["commit"]),
                          "insert_ms": 1000 * metrics.phases["insert_incl_fts"],
                          "per_row_ms": 1000 * metrics.phases["insert_incl_fts"] / 201,
                          "api_ms": api_ms, "phases": dict(metrics.phases), "rows": dict(metrics.rows)}
                rows = db._read_all("SELECT * FROM messages ORDER BY id")
                actual = hashlib.sha256(repr([tuple(row) for row in rows]).encode()).hexdigest()
                expected = expected or actual
                assert actual == expected, (loaded, name, "stored rows differ")
                active = [row for row in rows if row["active"]]
                assert len(active) == len(messages) == 201
                for row, msg in zip(active, messages):
                    assert row["id"] == msg["_row_id"]
                    assert msg["_db_row_snapshot"] == digest(row)
                for table in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
                    ids = [row[0] for row in db._read_all(f"SELECT rowid FROM {table} WHERE {table} MATCH 'benchword' ORDER BY rowid")]
                    assert ids == [row["id"] for row in rows]
                    db._write_sql(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
                if iteration >= 0:
                    samples[name].append(sample)
                print(json.dumps({"loaded": loaded, "iteration": iteration, "variant": name, **sample}), flush=True)

                def restore(conn):
                    conn.execute("DELETE FROM messages WHERE id > 400")
                    conn.execute("UPDATE sqlite_sequence SET seq=400 WHERE name='messages'")
                    conn.executemany("UPDATE messages SET active=?, compacted=?, display_identity=?, display_order=? WHERE id=?",
                                     [(a, c, i, o, rid) for rid, a, c, i, o in initial])
                db._execute_write(restore)
        return {"loaded": loaded, "samples": samples, "summary": {
            name: {key: {"median": statistics.median(row[key] for row in values),
                         "min": min(row[key] for row in values), "max": max(row[key] for row in values)}
                   for key in ("hold_ms", "insert_ms", "per_row_ms")}
            for name, values in samples.items()}}
    finally:
        transcript_repair.transcript_row_snapshot = new_hash
        db.close()
        assert sum(p.stat().st_size for p in directory.glob("*.sqlite*")) < 1024 ** 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 3:
        parser.error("at least 3 measured rounds required")
    with tempfile.TemporaryDirectory(prefix="compaction-insert-", dir=os.environ["TMPDIR"]) as temp:
        directory = Path(temp)
        # Bind isolation BEFORE any Hermes import, just like the test fixtures.
        os.environ["HERMES_HOME"] = str(directory / "home")
        os.environ["HERMES_TEST_ISOLATION"] = os.environ["HERMES_HOME"]
        library = directory / "home" / "lib" / "libfts5_cjk.so"
        library.parent.mkdir(parents=True)
        subprocess.run(["gcc", "-shared", "-fPIC", "-O2", f"-I{ROOT / 'native/fts5_cjk/vendor'}",
                        str(ROOT / "native/fts5_cjk/fts5_cjk.c"), "-o", str(library)], check=True)
        logging.getLogger("hermes_state").setLevel(logging.ERROR)
        result = {"baseline": args.baseline, "python": sys.version, "sqlite": sqlite3.sqlite_version,
                  "platform": platform.platform(), "switchinterval": sys.getswitchinterval(),
                  "rows": 201, "rounds": args.rounds,
                  "cases": [_case(directory, args.baseline, args.rounds, loaded) for loaded in (False, True)]}
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": [{"loaded": case["loaded"], "summary": case["summary"]} for case in result["cases"]]}, indent=2))


if __name__ == "__main__":
    main()
