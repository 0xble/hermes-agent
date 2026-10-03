"""Structural and focused behavioral coverage for ambient gateway deadlines."""
from __future__ import annotations

import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import time

import pytest

from gateway.deadline import check, connect_sqlite, deadline_scope, remaining, with_deadline_scope
from gateway.generation import GenerationCoordinator

ROOT = Path(__file__).parents[2]
BOUNDED_FILES = (
    "gateway/run_generation.py",
    "gateway/generation*.py",
    "gateway/owned_*.py",
    "hermes_cli/gateway_forward_update.py",
    "hermes_cli/gateway_guardian.py",
)
DEADLINE_CONSTANTS = {
    "STARTUP_SECONDS",
    "ROLLBACK_SECONDS",
    "POLL_SECONDS",
    "POLL_PROOF_SECONDS",
    "HANDOVER_REQUEST_TIMEOUT",
    "HANDOVER_ABORT_RESERVE",
    "COOPERATIVE_ROLLBACK_SECONDS",
}
# These are deadline plumbing helpers, not bounded entry points. Their callers
# install the scope and pass the value through to the coordinator operation.
DEADLINE_HELPERS = {
    "_deadline_kwargs",
    "_deadline_connect",
    "_check_transaction_deadline",
    "_begin_immediate",
    "_deadline_check",
    "_remaining",
}
DEADLINE_CALCULATORS = {"_proof_deadline"}


def _call_names(tree: ast.AST, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id == name)
                 or (isinstance(node.func, ast.Attribute) and node.func.attr == name))]


def _source_paths() -> list[Path]:
    paths: list[Path] = []
    for pattern in BOUNDED_FILES:
        paths.extend(sorted(ROOT.glob(pattern)))
    return list(dict.fromkeys(paths))


def _functions(path: Path) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _direct_body_nodes(node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Walk a function without treating nested callbacks as its scope."""
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield from ast.walk(statement)


def _has_deadline_scope(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(call, ast.Call)
        and ((isinstance(call.func, ast.Name) and call.func.id == "deadline_scope")
             or (isinstance(call.func, ast.Attribute) and call.func.attr == "deadline_scope"))
        for call in _direct_body_nodes(node)
    )


def _is_deadline_computation(node: ast.AST) -> bool:
    if not isinstance(node, ast.Assign):
        return False
    names = {target.id for target in node.targets if isinstance(target, ast.Name)}
    if not names & {"deadline", "request_deadline", "coordinator_deadline", "recovery_deadline", "reload_deadline"}:
        return False
    loaded = {child.id for child in ast.walk(node.value)
              if isinstance(child, ast.Name)}
    clocks = {"monotonic", "_now", "_REAL_MONOTONIC"}
    return bool(loaded & clocks and loaded & DEADLINE_CONSTANTS)


def _bounded_entries() -> list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    entries = []
    for path in _source_paths():
        for node in _functions(path):
            args = {arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
            direct = list(_direct_body_nodes(node))
            if node.name in DEADLINE_HELPERS or node.name in DEADLINE_CALCULATORS:
                continue
            if "deadline" in args or any(_is_deadline_computation(item) for item in direct):
                entries.append((path, node))
    return entries


def _decorated_with_deadline_scope(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(isinstance(dec, ast.Name) and dec.id == "with_deadline_scope"
               for dec in node.decorator_list)


def _production_calls(name: str) -> list[ast.Call]:
    calls = []
    for path in _source_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls.extend(_call_names(tree, name))
    return calls


def _call_has_deadline(call: ast.Call, function_name: str) -> bool:
    return (any(keyword.arg == "deadline" for keyword in call.keywords)
            or function_name == "_await_wedge_proof" and len(call.args) >= 4)


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


def test_expired_deadline_scope_rejects_bounded_entry_without_write():
    writes = []

    @with_deadline_scope
    def bounded_write(*, deadline=None):
        check()
        writes.append(True)

    with pytest.raises(TimeoutError, match="deadline"):
        bounded_write(deadline=time.monotonic() - 1)
    assert writes == []


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


def test_bounded_deadline_producers_use_canonical_clock():
    paths = [ROOT / relative for relative in (
        "gateway/run_generation.py",
        "gateway/generation.py",
        "hermes_cli/gateway_forward_update.py",
        "hermes_cli/gateway_guardian.py",
    )]
    violations = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        if "time.monotonic()" in source or "_REAL_MONOTONIC" in source:
            violations.append(str(path.relative_to(ROOT)))
    assert not violations, "bounded deadline producers bypass gateway.deadline.now: " + ", ".join(violations)


def test_every_bounded_entry_is_scoped_or_keyword_adapted():
    missing = []
    for path, node in _bounded_entries():
        if _has_deadline_scope(node):
            continue
        if _decorated_with_deadline_scope(node):
            calls = _production_calls(node.name)
            if calls and all(_call_has_deadline(call, node.name) for call in calls):
                continue
            if not calls and node.name == "record_poller_stopped":
                continue
        missing.append(f"{path.relative_to(ROOT)}:{node.name}:{node.lineno}")
    assert not missing, "bounded gateway entries without an ambient deadline scope: " + ", ".join(missing)


def test_transfer_requests_carry_the_socket_deadline():
    missing = []
    for path in (ROOT / "gateway" / "run_generation.py", ROOT / "hermes_cli" / "gateway_forward_update.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _call_names(tree, "_generation_request"):
            if not call.args or not isinstance(call.args[1], ast.Constant):
                continue
            if call.args[1].value not in {"transfer_requested", "transfer_aborted"}:
                continue
            params = next((keyword.value for keyword in call.keywords if keyword.arg == "params"), None)
            if not isinstance(params, ast.Dict):
                missing.append(f"{path.relative_to(ROOT)}:{call.lineno}")
                continue
            keys = {key.value for key in params.keys if isinstance(key, ast.Constant)}
            if "deadline" not in keys:
                missing.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert not missing, "transfer control requests missing params.deadline: " + ", ".join(missing)


def test_supervisor_transfer_abort_requests_carry_the_socket_deadline():
    missing = []
    path = ROOT / "hermes_cli" / "gateway_forward_update.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for call in _call_names(tree, "request"):
        if len(call.args) < 2 or not isinstance(call.args[1], ast.Constant):
            continue
        if call.args[1].value not in {"transfer_requested", "transfer_aborted"}:
            continue
        params = next((keyword.value for keyword in call.keywords if keyword.arg == "params"), None)
        keys = ({key.value for key in params.keys if isinstance(key, ast.Constant)}
                if isinstance(params, ast.Dict) else set())
        if "deadline" not in keys:
            missing.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert not missing, "supervisor transfer requests missing params.deadline: " + ", ".join(missing)


def test_unbounded_scope_is_not_used_for_gateway_recovery():
    uses = []
    for path in _source_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _call_names(tree, "unbounded_scope"):
            uses.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert not uses, "unbounded_scope is forbidden in bounded gateway paths: " + ", ".join(uses)


def test_long_lived_tasks_do_not_inherit_bounded_deadlines():
    files = [ROOT / "gateway" / "run_generation.py", ROOT / "gateway" / "run.py",
             ROOT / "gateway" / "owned_routing.py",
             ROOT / "plugins" / "platforms" / "telegram" / "adapter.py",
             ROOT / "plugins" / "platforms" / "telegram" / "polling_transfer.py"]
    violations = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (_has_deadline_scope(node) or _decorated_with_deadline_scope(node)):
                continue
            for call in _direct_body_nodes(node):
                if not isinstance(call, ast.Call):
                    continue
                name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
                if name in {"create_task", "ensure_future"} and not any(
                        keyword.arg == "context" for keyword in call.keywords):
                    violations.append(f"{path.relative_to(ROOT)}:{call.lineno}:{name}")
    assert not violations, "long-lived task spawn inherits a bounded deadline: " + ", ".join(violations)


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


def _spawn_context_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg == "context" and isinstance(keyword.value, ast.Call):
                func = keyword.value.func
                yield call.lineno, func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def test_detached_tasks_keep_non_deadline_context():
    """An empty Context() drops every other ContextVar (e.g. the Telegram polling
    generation that gates journaled wire commits); detach only the deadline."""
    files = [ROOT / "gateway" / "run_generation.py", ROOT / "gateway" / "run.py",
             ROOT / "plugins" / "platforms" / "telegram" / "adapter.py",
             ROOT / "plugins" / "platforms" / "telegram" / "polling_transfer.py"]
    bad = [f"{path.relative_to(ROOT)}:{line}" for path in files
           for line, name in _spawn_context_names(path) if name != "detached_context"]
    assert not bad, "task spawned with a context other than detached_context(): " + ", ".join(bad)


def test_detached_context_clears_only_the_deadline():
    from contextvars import ContextVar
    from gateway import deadline as gd

    marker: ContextVar[str | None] = ContextVar("detached_marker", default=None)
    token = marker.set("generation-7")
    try:
        with gd.deadline_scope(gd.now() - 1):
            ctx = gd.detached_context()
            assert gd.current() is not None
        assert ctx.run(gd.current) is None
        assert ctx.run(marker.get) == "generation-7"
    finally:
        marker.reset(token)
