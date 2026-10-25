#!/usr/bin/env python3
"""Compare real planned SQLite folds with a pinned baseline on one disposable database.

Run with the repository's test interpreter and a clean environment. The output
contains every sample and semantic digest. This is not a wall-time unit test.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_state import SessionDB
from hermes_state_messages import SessionMessagesMixin
from hermes_state_write_metrics import current_write_metrics


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=lambda b: b.hex()).encode()).hexdigest()


def snapshot(db):
    fold = db._read_all("SELECT id, display_order, display_identity FROM messages ORDER BY id")
    projection = db.get_messages("bench", include_compacted=True)
    result = {"fold": digest([tuple(row) for row in fold]),
              "visible_projection": digest(projection), "visible_rows": len(projection)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--payload-kib", type=int, default=64)
    parser.add_argument("--metadata-kib", type=int, default=128)
    parser.add_argument("--cache-kib", type=int, default=128)
    parser.add_argument("--repetitions", type=int, default=21)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = subprocess.check_output(
        ["git", "show", f"{args.baseline}:hermes_state_messages.py"], cwd=ROOT, timeout=30)
    baseline_sha = subprocess.check_output(["git", "rev-parse", args.baseline], cwd=ROOT, text=True, encoding="utf-8", timeout=30).strip()
    # Preserve the pinned baseline's actual methods, not a reimplementation of its SQL.
    with tempfile.TemporaryDirectory(prefix="display-fold-", dir=os.environ["TMPDIR"]) as scratch:
        source_path = Path(scratch) / "baseline_messages.py"
        source_path.write_bytes(source)
        spec = importlib.util.spec_from_file_location("baseline_messages", source_path)
        assert spec is not None and spec.loader is not None
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        db = SessionDB(Path(scratch) / "state.db")
        try:
            db._execute_write(lambda conn: conn.execute(f"PRAGMA cache_size = -{args.cache_kib}"))
            db.create_session("bench", source="test")
            payload = "x" * (args.payload_kib * 1024)
            padding = "m" * (args.metadata_kib * 1024)
            db.append_messages_batch("bench", [
                {"role": "user", "content": f"{i}: {payload}", "timestamp": float(i),
                 "display_kind": "hidden" if i % 23 == 0 else None,
                 "display_metadata": {"padding": padding, "model_only": i % 17 == 0,
                                      "reactions": [{"emoji": "heart", "count": i % 3}]}}
                for i in range(args.rows)])
            tail = db.get_messages_as_conversation("bench", include_row_ids=True)[-2:]
            db.archive_and_compact("bench", [{"role": "user", "content": "summary",
                                             "_compressed_summary": True}, *tail], tail_count=2)
            methods = ("_display_fold_plan", "_fold_display_orders", "_reconcile_display_orders")
            variants = {"baseline": baseline.SessionMessagesMixin, "candidate": SessionMessagesMixin}
            samples = {name: [] for name in variants}
            expected = snapshot(db)
            plans = {}
            plan_ms = {}
            for name, mixin in variants.items():
                start = time.perf_counter()
                plans[name] = mixin._display_fold_plan(db, "bench")
                plan_ms[name] = (time.perf_counter() - start) * 1000
            # Prove each variant's planned projection independently before timing it.
            for name, mixin in variants.items():
                for method in methods:
                    setattr(db, method, types.MethodType(getattr(mixin, method), db))
                db._execute_write(lambda conn, name=name: db._reconcile_display_orders(conn, "bench", plans[name]))
                assert snapshot(db) == expected
            # Alternate order each round. One warmup pair is excluded, no timing threshold in pytest.
            for repetition in range(-1, args.repetitions):
                order = list(variants) if repetition % 2 == 0 else list(reversed(variants))
                for name in order:
                    mixin = variants[name]
                    for method in methods:
                        setattr(db, method, types.MethodType(getattr(mixin, method), db))
                    phases = {}

                    def fold(conn):
                        start = time.perf_counter()
                        db._reconcile_display_orders(conn, "bench", plans[name])
                        phases["fold_ms"] = (time.perf_counter() - start) * 1000
                        metrics = current_write_metrics.get()
                        assert metrics is not None
                        phases["phases_ms"] = {key: value * 1000 for key, value in metrics.phases.items()}

                    start = time.perf_counter()
                    db._execute_write(fold)
                    phases["transaction_ms"] = (time.perf_counter() - start) * 1000
                    if repetition >= 0:
                        samples[name].append(phases)
            assert snapshot(db) == expected, "stored fold or visible metadata changed"
            # Verify both a planned and full fold from each implementation against the same projection.
            for mixin in variants.values():
                for method in methods:
                    setattr(db, method, types.MethodType(getattr(mixin, method), db))
                db._execute_write(lambda conn: db._reconcile_display_orders(conn, "bench"))
                assert snapshot(db) == expected
            assert db._conn is not None
            settings = {key: db._conn.execute(f"PRAGMA {key}").fetchone()[0]
                        for key in ("cache_size", "page_size", "journal_mode", "synchronous")}
            summary = {name: {metric: {"median": statistics.median(row[metric] for row in rows),
                                      "min": min(row[metric] for row in rows),
                                      "max": max(row[metric] for row in rows)}
                              for metric in ("fold_ms", "transaction_ms")}
                       for name, rows in samples.items()}
            reduction = 100 * (1 - summary["candidate"]["fold_ms"]["median"] /
                               summary["baseline"]["fold_ms"]["median"])
            result = {"baseline_sha": baseline_sha, "candidate_source_sha256": hashlib.sha256(
                (ROOT / "hermes_state_messages.py").read_bytes()).hexdigest(),
                "protocol": vars(args) | {"output": str(args.output)}, "settings": settings,
                "semantic_digest": expected, "plan_ms": plan_ms, "samples": samples,
                "summary": summary, "fold_reduction_percent": reduction}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"summary": summary, "fold_reduction_percent": reduction,
                              "semantic_digest": expected, "output": str(args.output)}, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    main()
