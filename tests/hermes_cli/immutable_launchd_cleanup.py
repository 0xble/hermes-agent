"""Cleanup registry for disposable immutable-release launchd tests."""
from __future__ import annotations

import atexit
import json
import os
import subprocess
from pathlib import Path

import psutil

_PREFIXES = ("ai.hermes.s2spike.", "ai.hermes.s2migration.", "ai.hermes.s2crash.")
_REGISTRY = "immutable-launchd-labels.jsonl"


def _sweep(registry: Path) -> None:
    try:
        entries = [json.loads(line) for line in registry.read_text().splitlines()]
    except (OSError, ValueError):
        return
    for entry in entries:
        label, path = entry.get("label", ""), Path(entry.get("plist", "/"))
        if not label.startswith(_PREFIXES) or not path.name == f"{label}.plist":
            continue
        target = f"gui/{os.getuid()}/{label}"
        subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)


def register_disposable_label(request, label: str, plist: Path) -> None:
    """Persist before bootstrap; a subsequent session can recover a killed worker."""
    assert label.startswith(_PREFIXES) and plist.name == f"{label}.plist"
    root = Path(request.config._tmp_path_factory.getbasetemp())
    registry = root / _REGISTRY
    entry = {"label": label, "plist": str(plist), "pid": os.getpid(),
             "created": psutil.Process().create_time()}
    with registry.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    atexit.register(_sweep, registry)
    request.addfinalizer(lambda: _sweep(registry))


def sweep_prior_sessions(request) -> None:
    """Only reap registrations whose exact worker identity has exited."""
    base = Path(request.config._tmp_path_factory.getbasetemp()).parent.parent.parent
    try:
        registries = list(base.glob(f"r-*/pytest-of-*/pytest-*/{_REGISTRY}"))
    except OSError:
        # Another isolated test runner may remove its temp tree during glob.
        registries = []
    for registry in registries:
        try:
            entries = [json.loads(line) for line in registry.read_text().splitlines()]
        except (OSError, ValueError):
            continue
        if entries and all(not psutil.pid_exists(item["pid"]) or
                           psutil.Process(item["pid"]).create_time() != item["created"]
                           for item in entries):
            _sweep(registry)
            registry.unlink(missing_ok=True)
