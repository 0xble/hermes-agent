#!/usr/bin/env python3
"""Slice 13 schema-compatibility rehearsal on a COPY of a legacy profile's ``state.db``.

Never run against a live database. Given a consistent copy (taken with SQLite's backup API while
the legacy gateway runs), this script:

1. records the copy's identity: size, ``schema_version``, table list, row count per table;
2. classifies every table as candidate-declared (created by this checkout's code), or legacy-only
   (nothing in this checkout creates it), with its row count, so pending work in fork-only tables
   is visible before anyone opens the file with the candidate;
3. opens the copy with the candidate's ``SessionDB`` under a scratch ``HERMES_HOME`` and records
   what changed: ``schema_version`` after open, tables added, tables dropped, columns added, and
   whether every legacy-only table and its rows survived untouched;
4. runs the same read-side probes before and after: sessions count, messages count, three FTS
   phrases, delivery ledger states, async delegation states;
5. optionally runs ``PRAGMA integrity_check`` (slow on a 30 GB file; opt in with ``--integrity``).

Exit 0 when the candidate opened the copy without dropping any table or row, and the read probes
match before and after. Exit 1 otherwise, with the differences printed. The copy is modified by
step 3 (that is the point of a rehearsal), so pass a disposable copy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

def _resolve_repo() -> Path:
    """The Hermes checkout this script operates on: the installed hermes_cli package's parent.

    Deriving it from __file__ broke the moment the installer copied this script into
    $HERMES_HOME/scripts (the cron --script root), where parents[1] is the profile home."""
    try:
        import hermes_cli
        return Path(hermes_cli.__file__).resolve().parents[1]
    except Exception:
        return Path(__file__).resolve().parents[1]


REPO = _resolve_repo()
FTS_PROBES = ("hermes", "telegram", "backup")


def declared_tables() -> set[str]:
    names: set[str] = set()
    # Core tables use bare CREATE TABLE; auxiliary ones use IF NOT EXISTS; FTS uses VIRTUAL TABLE.
    pattern = re.compile(r"CREATE (?:VIRTUAL )?TABLE (?:IF NOT EXISTS )?\"?([A-Za-z_][A-Za-z0-9_]*)", re.I)
    for path in REPO.rglob("*.py"):
        if "/tests/" in str(path) or "/.venv/" in str(path) or "/.worktrees/" in str(path):
            continue
        try:
            names.update(pattern.findall(path.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue
    return names


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def snapshot(path: Path) -> dict:
    with _ro(path) as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        info = {}
        for t in tables:
            cols = [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
            try:
                rows = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.DatabaseError as exc:
                rows = f"err {exc}"
            info[t] = {"columns": cols, "rows": rows}
        try:
            version = [r[0] for r in c.execute("SELECT version FROM schema_version")]
        except sqlite3.DatabaseError:
            version = []
        return {"size": path.stat().st_size, "schema_version": version, "tables": info}


def probes(path: Path) -> dict:
    out: dict = {}
    with _ro(path) as c:
        def q(sql, *a):
            try:
                return c.execute(sql, a).fetchall()
            except sqlite3.DatabaseError as exc:
                return f"err {exc}"
        out["sessions"] = q("SELECT COUNT(*) FROM sessions")
        out["messages"] = q("SELECT COUNT(*) FROM messages")
        out["delivery_states"] = q("SELECT state, COUNT(*) FROM delivery_obligations GROUP BY state ORDER BY state")
        out["delegation_states"] = q("SELECT state, COUNT(*) FROM async_delegations GROUP BY state ORDER BY state")
        for phrase in FTS_PROBES:
            out[f"fts:{phrase}"] = q("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", phrase)
    return out


def open_with_candidate(copy: Path, scratch_home: Path, python: str) -> dict:
    """Open the copy through the candidate's SessionDB in a fresh process under a scratch home."""
    scratch_home.mkdir(parents=True, exist_ok=True)
    target = scratch_home / "state.db"
    if target.exists() or target.is_symlink():
        target.unlink()
    os.symlink(copy, target)
    code = (
        "import json, time; t=time.time(); import hermes_state; db=hermes_state.SessionDB(); "
        "n=len(db.list_sessions_rich(limit=5)) if hasattr(db,'list_sessions_rich') else None; "
        "print(json.dumps({'opened': True, 'seconds': round(time.time()-t,2), 'listed': n}))"
    )
    env = {**os.environ, "HERMES_HOME": str(scratch_home), "HERMES_STATE_DB_GUARD_BYPASS": "1"}
    env.pop("PYTHONPATH", None)
    run = subprocess.run([python, "-c", code], cwd=str(REPO), env=env, capture_output=True, text=True, timeout=3600)
    try:
        result = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = {"opened": False}
    result["exit"] = run.returncode
    result["stderr_tail"] = run.stderr.strip()[-1200:]
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("copy", type=Path, help="a DISPOSABLE copy of state.db")
    ap.add_argument("--scratch-home", type=Path, default=Path("/tmp/hn-schema-rehearsal-home"))
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--integrity", action="store_true", help="run PRAGMA integrity_check on the copy first (slow)")
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv)
    copy = args.copy.resolve()
    if not copy.is_file():
        raise SystemExit(f"{copy} is not a file")
    if str(copy).startswith(str(Path.home() / ".hermes")):
        raise SystemExit("refusing: that path is inside the live profile; pass a copy")

    report: dict = {"copy": str(copy), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if args.integrity:
        t = time.time()
        with _ro(copy) as c:
            report["integrity_check"] = {"result": c.execute("PRAGMA integrity_check").fetchall()[:5], "seconds": round(time.time() - t, 1)}
    before = snapshot(copy)
    declared = declared_tables()
    legacy_only = {t: v["rows"] for t, v in before["tables"].items()
                   if t not in declared and not re.search(r"_(fts|config|data|docsize|idx|content)$", t)}
    report["before"] = {"size": before["size"], "schema_version": before["schema_version"], "tables": len(before["tables"]),
                        "legacy_only_tables": legacy_only,
                        "legacy_only_rows": sum(v for v in legacy_only.values() if isinstance(v, int))}
    report["probes_before"] = probes(copy)
    report["open"] = open_with_candidate(copy, args.scratch_home, args.python)
    after = snapshot(copy)
    report["after"] = {"size": after["size"], "schema_version": after["schema_version"], "tables": len(after["tables"])}
    report["probes_after"] = probes(copy)

    dropped = sorted(set(before["tables"]) - set(after["tables"]))
    added = sorted(set(after["tables"]) - set(before["tables"]))
    columns_added = {t: sorted(set(after["tables"][t]["columns"]) - set(before["tables"][t]["columns"]))
                     for t in before["tables"] if t in after["tables"]}
    columns_added = {t: c for t, c in columns_added.items() if c}
    # Opening a store legitimately stamps bookkeeping keys (store_instance_id, db_file_generation)
    # into state_meta; that is not data loss. Every other table must hold exactly what it held.
    rows_changed = {t: (before["tables"][t]["rows"], after["tables"][t]["rows"]) for t in before["tables"]
                    if t in after["tables"] and before["tables"][t]["rows"] != after["tables"][t]["rows"]
                    and t != "state_meta"}
    meta_before = before["tables"].get("state_meta", {}).get("rows")
    meta_after = after["tables"].get("state_meta", {}).get("rows")
    if isinstance(meta_before, int) and isinstance(meta_after, int) and meta_after < meta_before:
        rows_changed["state_meta"] = (meta_before, meta_after)
    report["diff"] = {"tables_dropped": dropped, "tables_added": added, "columns_added": columns_added, "rows_changed": rows_changed}
    probe_mismatch = {k: (report["probes_before"][k], report["probes_after"][k])
                      for k in report["probes_before"] if report["probes_before"][k] != report["probes_after"][k]}
    report["probe_mismatch"] = probe_mismatch
    ok = bool(report["open"].get("opened")) and not dropped and not rows_changed and not probe_mismatch
    report["verdict"] = "compatible" if ok else "incompatible"
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
