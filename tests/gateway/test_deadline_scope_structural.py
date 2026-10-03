"""Structural and focused behavioral coverage for ambient gateway deadlines."""
from __future__ import annotations

import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import time

import pytest

from gateway.deadline import connect_sqlite, deadline_scope, remaining
from gateway.generation import GenerationCoordinator

ROOT = Path(__file__).parents[2]


def _call_names(tree: ast.AST, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id == name)
                 or (isinstance(node.func, ast.Attribute) and node.func.attr == name))]


def test_nested_scope_keeps_tighter_deadline_and_restores_parent():
    outer = time.monotonic() + 10
    inner = time.monotonic() + 2
    with deadline_scope(outer):
        assert remaining() is not None and remaining() <= 10
        with deadline_scope(inner) as effective:
            assert effective == inner
            assert remaining() <= 2
        assert remaining() > 0
    assert remaining() is None


def test_expired_scope_rejects_sqlite_connect_without_opening():
    with pytest.raises(TimeoutError, match="deadline"):
        with deadline_scope(time.monotonic() - 1):
            connect_sqlite(":memory:")


def test_coordinator_connection_uses_ambient_busy_timeout(tmp_path):
    db = GenerationCoordinator(tmp_path)
    with deadline_scope(time.monotonic() + 0.25):
        with closing(db.connect(timeout=5)) as conn:
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert 0 < busy_timeout <= 250


def test_coordinator_write_transactions_route_through_one_helper():
    source = (ROOT / "gateway" / "generation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    direct = []
    for node in _call_names(tree, "execute"):
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "BEGIN IMMEDIATE":
            direct.append(node.lineno)
    assert len(direct) == 1, f"unexpected direct coordinator BEGIN IMMEDIATE at lines {direct}"
    assert _call_names(tree, "begin_immediate"), "coordinator must use the centralized helper"


def test_bounded_entries_use_scope_or_scope_adapter():
    entries = {
        "gateway/run_generation.py": (
            "handover_to_generation", "transfer_requested", "transfer_aborted",
            "serve_standby_generation",
        ),
        "hermes_cli/gateway_forward_update.py": (
            "_poller", "_await_wedge_proof", "_terminate_proven_wedged", "_rollback",
        ),
        "hermes_cli/gateway_guardian.py": ("_run", "_repair_parked", "rollback_switch"),
    }
    for relative, names in entries.items():
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        functions = {node.name: node for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in names:
            node = functions[name]
            has_call = any(
                isinstance(call, ast.Call)
                and ((isinstance(call.func, ast.Name) and call.func.id == "deadline_scope")
                     or (isinstance(call.func, ast.Attribute) and call.func.attr == "deadline_scope"))
                for call in ast.walk(node)
            )
            decorated = any(
                isinstance(dec, ast.Name) and dec.id == "with_deadline_scope"
                for dec in node.decorator_list
            )
            assert has_call or decorated, f"{relative}:{name} has no deadline scope"


@pytest.mark.parametrize("relative", [
    "gateway/generation.py",
    "gateway/run_generation.py",
    "gateway/owned_routing.py",
    "hermes_cli/gateway_forward_update.py",
])
def test_bounded_state_openers_use_connect_helper(relative):
    source = (ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    direct = []
    for node in _call_names(tree, "connect"):
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id == "sqlite3":
                direct.append(node.lineno)
    assert not direct, f"direct sqlite3.connect outside ambient helper at lines {direct}"
