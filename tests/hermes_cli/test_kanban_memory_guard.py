"""Memory-aware kanban dispatch guard (OOF-30 / OOF-77).

Two production incidents shared the same failure shape: no
``kanban.max_in_progress`` configured, a busy board, and a small hosted VM —
the dispatcher fanned out 26-31 concurrent workers, the host swap-thrashed,
and the whole machine (dashboard included) became unreachable.

Covers the live memory-pressure guard inside ``dispatch_once``: critical
pressure spawns nothing, elevated pressure spawns at most one, unknown imposes
no restriction (fail-open). The host cap itself defaults to
``DEFAULT_MAX_IN_PROGRESS`` (``test_kanban_host_cap.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


GIB = 1024 * 1024  # KiB per GiB


# ---------------------------------------------------------------------------
# _memory_pressure_level
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# dispatch_once under pressure
# ---------------------------------------------------------------------------


def _pressure_sample(level: str) -> dict:
    total = 1 * GIB
    if level == "critical":
        return {"mem_available_kib": 32 * 1024, "mem_total_kib": total}
    if level == "elevated":
        return {"mem_available_kib": 100 * 1024, "mem_total_kib": total}
    return {"mem_available_kib": total // 2, "mem_total_kib": total}


def test_dispatch_spawns_nothing_under_critical_pressure(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(
        kbd, "_system_memory_sample", lambda: _pressure_sample("critical")
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert not spawns
    assert not res.spawned
    assert res.memory_pressure == "critical"


def test_dispatch_critical_pressure_defers_not_drops(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Tasks skipped under pressure stay 'ready' and spawn once memory clears."""
    sample = {"value": _pressure_sample("critical")}
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: sample["value"])
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        task = kb.create_task(conn, title="a", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert not spawns
        row = kb.get_task(conn, task)
        assert row is not None and row.status == "ready"

        sample["value"] = _pressure_sample("ok")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [task]
    assert res.memory_pressure is None


def test_dispatch_elevated_pressure_spawns_at_most_one(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(
        kbd, "_system_memory_sample", lambda: _pressure_sample("elevated")
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 1
    assert res.memory_pressure == "elevated"


def test_dispatch_elevated_pressure_does_not_widen_tighter_budget(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A caller cap already at 0 remaining must not be widened to 1."""
    monkeypatch.setattr(
        kbd, "_system_memory_sample", lambda: _pressure_sample("elevated")
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        kb.claim_task(conn, running)
        kb.create_task(conn, title="ready", assignee="bob")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=1)

    assert not spawns
    assert not res.spawned


def test_dispatch_unknown_pressure_imposes_no_restriction(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 3
    assert res.memory_pressure is None


def test_dispatch_critical_pressure_still_runs_reclaim_bookkeeping(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The guard must only stop NEW spawns — reclaim/promotion still run."""
    monkeypatch.setattr(
        kbd, "_system_memory_sample", lambda: _pressure_sample("critical")
    )
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(
            conn, title="child", assignee="alice", parents=[parent],
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 42)
        row = kb.get_task(conn, child)

    # Promotion (todo -> ready once parents are done) happened despite the
    # spawn freeze.
    assert res.memory_pressure == "critical"
    assert row is not None
    assert row.status == "ready"
