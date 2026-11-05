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
    if _STATE is not None:
        _STATE["reports"].append({
            "node": report.nodeid, "phase": report.when, "outcome": report.outcome,
            "category": call.excinfo.typename if call.excinfo else "",
        })


def pytest_sessionfinish(session, exitstatus):
    if _STATE is not None:
        _STATE["exit"] = int(exitstatus)
        directory = Path(session.config.getoption("--gate-evidence-dir"))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{uuid.uuid4().hex}.json").write_text(json.dumps(_STATE), encoding="utf-8")
