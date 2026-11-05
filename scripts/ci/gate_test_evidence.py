"""Pytest's machine-readable node/phase evidence for the frozen gate harness."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest


_STATE = None


def pytest_addoption(parser):
    parser.addoption("--gate-evidence-dir", default=None)


def pytest_configure(config):
    global _STATE
    if config.getoption("--gate-evidence-dir"):
        _STATE = {"files": config.args, "collected": [], "reports": [], "errors": []}


def pytest_collection_finish(session):
    if _STATE is not None:
        _STATE["collected"] = [item.nodeid for item in session.items]


def pytest_collectreport(report):
    if _STATE is not None and report.failed:
        _STATE["errors"].append(report.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    report._gate_category = call.excinfo.typename if call.excinfo else ""


def pytest_runtest_logreport(report):
    if _STATE is not None:
        # Pytest invokes makereport for each unittest subTest *and* its parent.
        # Observe the final emitted report instead: subreports have context,
        # while the parent has exactly one setup/call/teardown. Failing/skipped
        # subtests have no independent collected node ID and fail closed.
        if hasattr(report, "context"):
            if report.outcome != "passed":
                _STATE["errors"].append(f"Non-passing subtest: {report.nodeid}")
            return
        _STATE["reports"].append({
            "node": report.nodeid, "phase": report.when, "outcome": report.outcome,
            "category": getattr(report, "_gate_category", ""),
        })


def pytest_sessionfinish(session, exitstatus):
    if _STATE is not None:
        _STATE["exit"] = int(exitstatus)
        directory = Path(session.config.getoption("--gate-evidence-dir"))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{uuid.uuid4().hex}.json").write_text(json.dumps(_STATE), encoding="utf-8")
