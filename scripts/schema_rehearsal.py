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

Exit 0 when the candidate opened cleanly without canonical table/row/column loss, preserved
metadata, and retained read probes. A v2->v3 base FTS migration may change derived rows/search
only after canonical projection and rank-1 integrity verification. Exit 1 otherwise. The copy is modified by
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
import tempfile
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
        if {"tests", ".venv", "venv", ".worktrees"}.intersection(path.relative_to(REPO).parts[:-1]):
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
        meta = dict(c.execute("SELECT key, value FROM state_meta")) if "state_meta" in info else {}
        ddl = dict(c.execute("SELECT name, sql FROM sqlite_master WHERE name IN ('messages_fts', 'messages_fts_src')"))
        shadows = {r[1] for r in c.execute("PRAGMA table_list") if r[2] == "shadow"}
        return {"size": path.stat().st_size, "schema_version": version, "tables": info,
                "meta": meta, "fts_ddl": ddl, "shadows": sorted(shadows)}


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
    run = subprocess.run([python, "-c", code], cwd=str(REPO), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    try:
        result = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = {"opened": False}
    result["exit"] = run.returncode
    result["stderr_tail"] = run.stderr.strip()[-1200:]
    return result



def verify_aligned_fts(path: Path) -> dict:
    """Verify the declared projection AND actual index on the disposable copy, never rebuild it."""
    from hermes_state_common import FTS_TOOL_CONTENT_PREFIX_CHARS
    try:
        with sqlite3.connect(str(path)) as c:
            expected = ("CASE WHEN m.role='tool' THEN substr(COALESCE(m.content,''),1,"
                        f"{FTS_TOOL_CONTENT_PREFIX_CHARS}) ELSE m.content END")
            # NULL-safe joined comparisons avoid sorting multi-GB message bodies.
            mismatch = c.execute(f"""SELECT 1 FROM messages m
                LEFT JOIN messages_fts_src v ON v.id=m.id
                WHERE v.id IS NULL OR v.content IS NOT ({expected})
                   OR v.tool_name IS NOT m.tool_name OR v.tool_calls IS NOT m.tool_calls
                LIMIT 1""").fetchone()
            extra = c.execute("""SELECT 1 FROM messages_fts_src v
                LEFT JOIN messages m ON m.id=v.id WHERE m.id IS NULL LIMIT 1""").fetchone()
            duplicate = c.execute("""SELECT id FROM messages_fts_src
                GROUP BY id HAVING COUNT(*) != 1 LIMIT 1""").fetchone()
            if mismatch or extra or duplicate:
                return {"ok": False, "error": "base FTS source differs from canonical projection"}
            c.execute("BEGIN")
            try:
                c.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")
            finally:
                c.rollback()
        return {"ok": True, "projection": "canonical", "integrity": "rank=1"}
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "error": str(exc)}


def assess(path: Path, before: dict, after: dict, pre: dict, post: dict,
           opened: dict, integrity: dict | None = None) -> dict:
    """Keep canonical checks strict; only independently verified v2->v3 derived changes are expected."""
    errors = []
    for label, snap, probe in (("before", before, pre), ("after", after, post)):
        errors.extend(f"{label}: {name}: {v['rows']}" for name, v in snap["tables"].items()
                      if not isinstance(v["rows"], int))
        errors.extend(f"{label}: {name}: {value}" for name, value in probe.items()
                      if isinstance(value, str))
    dropped = sorted(set(before["tables"]) - set(after["tables"]))
    added = sorted(set(after["tables"]) - set(before["tables"]))
    common = before["tables"].keys() & after["tables"].keys()
    rows = {t: (before["tables"][t]["rows"], after["tables"][t]["rows"]) for t in common
            if before["tables"][t]["rows"] != after["tables"][t]["rows"] and t != "state_meta"}
    columns_removed = {t: sorted(set(before["tables"][t]["columns"]) - set(after["tables"][t]["columns"])) for t in common}
    columns_added = {t: sorted(set(after["tables"][t]["columns"]) - set(before["tables"][t]["columns"])) for t in common}
    mismatch = {k: (pre.get(k), post.get(k)) for k in pre.keys() | post.keys() if pre.get(k) != post.get(k)}
    bm, am = before["meta"], after["meta"]
    removed_meta = sorted(bm.keys() - am.keys())
    changed_meta = {k: (bm[k], am[k]) for k in bm.keys() & am.keys() if bm[k] != am[k]}
    # These existing bookkeeping keys may be refreshed by opening a copied store.
    for key in ("store_instance_id", "db_file_generation"):
        changed_meta.pop(key, None)
    transition = (bm.get("fts_storage_version") == "2" and am.get("fts_storage_version") == "3"
                  and bool(re.search(r"content\s*=\s*['\"]messages['\"]", before["fts_ddl"].get("messages_fts", ""), re.I))
                  and bool(re.search(r"content\s*=\s*['\"]messages_fts_src['\"]", after["fts_ddl"].get("messages_fts", ""), re.I)))
    proof = verify_aligned_fts(path) if transition else {"ok": False, "reason": "not the supported v2->v3 transition"}
    expected = {"rows_changed": {}, "probe_mismatch": {}, "metadata_removed": [], "metadata_changed": {}}
    if transition and proof["ok"]:
        base_shadows = {"messages_fts_" + suffix for suffix in ("data", "idx", "content", "docsize", "config")}
        allowed = base_shadows & set(before["shadows"]) & set(after["shadows"])
        for name in list(rows):
            if name in allowed:
                expected["rows_changed"][name] = rows.pop(name)
        for name in list(mismatch):
            if name in {"fts:" + phrase for phrase in FTS_PROBES}:
                expected["probe_mismatch"][name] = mismatch.pop(name)
        for key in ("fts_tool_full_content_high_water", "fts_rebuild_high_water", "fts_rebuild_progress"):
            if key in removed_meta:
                removed_meta.remove(key)
                expected["metadata_removed"].append(key)
        if "fts_storage_version" in changed_meta:
            expected["metadata_changed"]["fts_storage_version"] = changed_meta.pop("fts_storage_version")
    elif transition:
        errors.append("v2->v3 validation failed: " + str(proof.get("error")))
    if integrity is not None and integrity.get("result") not in ([["ok"]], [("ok",)]):
        errors.append("requested SQLite integrity_check failed")
    diff = {"tables_dropped": dropped, "tables_added": added,
            "columns_added": {k: v for k, v in columns_added.items() if v},
            "columns_removed": {k: v for k, v in columns_removed.items() if v},
            "rows_changed": rows, "metadata_removed": removed_meta, "metadata_changed": changed_meta}
    ok = (opened.get("opened") is True and opened.get("exit") == 0 and not errors
          and not dropped and not rows and not diff["columns_removed"]
          and not removed_meta and not changed_meta and not mismatch)
    return {"ok": ok, "diff": diff, "probe_mismatch": mismatch,
            "expected_derived_changes": expected, "fts_transition_validation": proof, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("copy", type=Path, help="a DISPOSABLE copy of state.db")
    ap.add_argument("--scratch-home", type=Path, help="scratch profile directory (default: a unique temporary directory)")
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
    scratch_home = args.scratch_home or Path(tempfile.mkdtemp(prefix="hn-schema-rehearsal-"))
    report["scratch_home"] = str(scratch_home)
    report["open"] = open_with_candidate(copy, scratch_home, args.python)
    after = snapshot(copy)
    report["after"] = {"size": after["size"], "schema_version": after["schema_version"], "tables": len(after["tables"])}
    report["probes_after"] = probes(copy)

    assessment = assess(copy, before, after, report["probes_before"], report["probes_after"],
                        report["open"], report.get("integrity_check"))
    ok = assessment.pop("ok")
    report.update(assessment)
    report["verdict"] = "compatible" if ok else "incompatible"
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
