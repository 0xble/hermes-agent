"""Sequential command budgets and explicitly unbounded PM preparation phases."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

UV_STEP_TIMEOUT = 1800
VENV_CREATE_TIMEOUT = 120
DEPENDENCY_CHECK_TIMEOUT = 60
WORKSPACE_PROBE_TIMEOUT = 60
REPAIR_CHECK_TIMEOUT = 60
PROTOCOL_MARGIN = 180
SYNC_ATTEMPTS = 2  # Initial sync plus the bytecode-compilation fallback.

_reporter = ContextVar("pm_worker_budget_reporter", default=None)
_unbounded_depth = ContextVar("pm_worker_unbounded_depth", default=0)


def build_budget(timeout=UV_STEP_TIMEOUT, *, frozen=False):
    lock = 0 if frozen else timeout
    return VENV_CREATE_TIMEOUT + lock + SYNC_ATTEMPTS * timeout + DEPENDENCY_CHECK_TIMEOUT


def operation_budget(operation, arguments):
    timeout = arguments.get("timeout", UV_STEP_TIMEOUT)
    if operation == "build_environment":
        return build_budget(timeout, frozen=arguments.get("frozen", True)) + PROTOCOL_MARGIN
    if operation in ("ensure_environment", "ensure_python_tool", "ensure_project_environment"):
        return build_budget(timeout, frozen=operation == "ensure_project_environment") + PROTOCOL_MARGIN
    if operation == "sync_venv" or (operation == "ensure" and arguments.get("name") == "venv"):
        return build_budget() + WORKSPACE_PROBE_TIMEOUT + REPAIR_CHECK_TIMEOUT + PROTOCOL_MARGIN
    if operation == "build_requirements_environment":
        return VENV_CREATE_TIMEOUT + UV_STEP_TIMEOUT + DEPENDENCY_CHECK_TIMEOUT + PROTOCOL_MARGIN
    if operation in ("lock_project", "check_project_lock", "export_requirements", "prune_cache"):
        return UV_STEP_TIMEOUT + PROTOCOL_MARGIN
    return PROTOCOL_MARGIN


@contextmanager
def reporting(send):
    from pm.environments import _patient_lock_wait

    token = _reporter.set(send)
    wait_token = _patient_lock_wait.set(unbounded)
    try:
        yield
    finally:
        _patient_lock_wait.reset(wait_token)
        _reporter.reset(token)


@contextmanager
def unbounded():
    """Patient lock waits and progressing tool acquisition have no total limit."""
    report = _reporter.get()
    depth = _unbounded_depth.get()
    token = _unbounded_depth.set(depth + 1)
    if report is not None and depth == 0:
        report({"type": "deadline", "suspended": True})
    try:
        yield
    finally:
        _unbounded_depth.reset(token)
        if report is not None and depth == 0:
            report({"type": "deadline", "suspended": False})


def reserve_eviction_builds(plugin_count):
    # One combined attempt, base build, up to two trials per actual member,
    # core-only recovery, and final commit: at most 2N + 4 builds in total.
    # The initial operation budget already covers one build.
    report = _reporter.get()
    if report is not None:
        report({"type": "deadline", "additional": (2 * plugin_count + 3)
                * (build_budget() + WORKSPACE_PROBE_TIMEOUT)})
