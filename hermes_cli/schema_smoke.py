"""Pre-activation proof that a candidate's SQLite initializers converge on this profile's schemas.

An initializer that decides to rebuild a table from the stored definition must stop once the
rebuild is done. Upstream's v0.23.1 status migration matched only upstream's stored text, so on
this fork's execution ledger it rebuilt the table on every open: indexes dropped mid-copy, a
multi-gigabyte WAL, and cron writes failing until a hotfix shipped. The candidate's own test
suite cannot see that, because the shape that breaks it exists only in the installed profile.

``check(home)`` copies each known database's *schema* (never its rows) into a scratch home, opens
it twice with the candidate's code, and reports any database whose schema is still changing on
the second open. The first open may migrate; the second must be a no-op.

Run by ``hermes update`` staging in the candidate's interpreter with ``HERMES_HOME`` pointed at
the scratch home: ``python -m hermes_cli.schema_smoke --source-home <profile>``.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Callable

# Row-carrying tables an initializer reads to decide what to migrate; tiny, and never user data.
_COPIED_ROWS = ("schema_version",)
_SHADOW_SUFFIX = re.compile(r"_(data|idx|content|docsize|config)$")


def _open_executions() -> None:
    from cron import executions
    executions._connect().close()


def _open_deliveries() -> None:
    from cron import delivery_queue
    delivery_queue._connect().close()


def _open_notepad() -> None:
    from cron import notepad
    notepad._connect().close()


def _open_outbox() -> None:
    from gateway.outbox import Outbox
    from hermes_constants import get_hermes_home
    Outbox(get_hermes_home())


def _open_state() -> None:
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    SessionDB(db_path=get_hermes_home() / "state.db").close()


# Path under the profile home -> open the database once through the code that owns its schema.
DATABASES: dict[str, Callable[[], None]] = {
    "cron/executions.db": _open_executions,
    "cron/deliveries.db": _open_deliveries,
    "cron/notepad.db": _open_notepad,
    "gateway-outbox.db": _open_outbox,
    "state.db": _open_state,
}


def copy_schema(source: Path, target: Path) -> None:
    """Recreate *source*'s schema, and the rows of its version table, in a new empty *target*."""
    target.parent.mkdir(parents=True, exist_ok=True)
    for stale in (target, target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
        stale.unlink(missing_ok=True)
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    try:
        objects = reader.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END, rowid"
        ).fetchall()
        virtual = {name for kind, name, sql in objects
                   if kind == "table" and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")}
        rows = {table: reader.execute(f"SELECT * FROM {table}").fetchall()
                for table in _COPIED_ROWS if any(name == table for _, name, _ in objects)}
    finally:
        reader.close()
    writer = sqlite3.connect(target)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        for kind, name, sql in objects:
            # A virtual table creates its own shadow tables; creating them first makes it fail.
            if kind == "table" and _SHADOW_SUFFIX.sub("", name) in virtual and name not in virtual:
                continue
            writer.execute(sql)
        for table, values in rows.items():
            for row in values:
                writer.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(row))})", row)
        writer.commit()
    finally:
        writer.close()


def snapshot(path: Path) -> tuple[int, list[tuple]]:
    """The schema cookie and every schema object with its root page: any rebuild moves one."""
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        return (reader.execute("PRAGMA schema_version").fetchone()[0],
                reader.execute("SELECT type, name, rootpage, sql FROM sqlite_master ORDER BY type, name").fetchall())
    finally:
        reader.close()


def _describe(before: tuple[int, list[tuple]], after: tuple[int, list[tuple]]) -> str:
    old = {(kind, name): (root, sql) for kind, name, root, sql in before[1]}
    new = {(kind, name): (root, sql) for kind, name, root, sql in after[1]}
    changed = sorted(f"{kind} {name}" for (kind, name) in old.keys() | new.keys()
                     if old.get((kind, name)) != new.get((kind, name)))
    return ", ".join(changed[:6]) or f"schema cookie {before[0]} -> {after[0]}"


def check(source_home: Path, scratch_home: Path) -> tuple[list[str], list[str]]:
    """``(failures, skipped)`` for the databases present under *source_home*.

    A failure is a schema still changing on the second open. Anything that stops the probe itself
    (an unreadable source, an initializer that raises on an empty copy) is ``skipped``: it proves
    nothing either way, so it is reported and never blocks an update.
    """
    failures: list[str] = []
    skipped: list[str] = []
    for relative, open_once in DATABASES.items():
        source = source_home / relative
        if not source.is_file():
            continue
        target = scratch_home / relative
        try:
            copy_schema(source, target)
            open_once()
            settled = snapshot(target)
            open_once()
            reopened = snapshot(target)
        except Exception as exc:  # the probe failed, not the candidate
            skipped.append(f"{relative}: {type(exc).__name__}: {exc}")
            continue
        if reopened != settled:
            failures.append(f"{relative} schema is still changing on the second open "
                            f"({_describe(settled, reopened)}); its migration would repeat on every open")
    return failures, skipped


def main(argv: list[str]) -> int:
    source_home = Path(argv[argv.index("--source-home") + 1])
    failures, skipped = check(source_home, Path(os.environ["HERMES_HOME"]))
    for line in skipped:
        print(f"schema smoke skipped {line}", file=sys.stderr)
    for line in failures:
        print(line, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
