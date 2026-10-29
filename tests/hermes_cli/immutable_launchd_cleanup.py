"""Cleanup registry for disposable immutable-release launchd tests."""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import shutil
from pathlib import Path

import psutil

_PREFIXES = ("ai.hermes.s2spike.", "ai.hermes.s2migration.", "ai.hermes.s2crash.")
_REGISTRY = "immutable-launchd-labels.jsonl"


def install_probe_process_dependency(venv: Path) -> None:
    """Give disposable minimal interpreters the same process observer used by the runtime."""
    python = venv / "bin/python"
    result = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    shutil.copytree(Path(psutil.__file__).parent, Path(result.stdout.strip()) / "psutil")


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


def _sweep_missing_plists(base: Path) -> None:
    """Recover disposable jobs after pytest has removed their registry and plist."""
    domain = f"gui/{os.getuid()}"
    listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
    if listing.returncode:
        return
    for row in listing.stdout.splitlines():
        columns = row.split("\t")
        if len(columns) != 3 or not columns[2].startswith(_PREFIXES):
            continue
        label = columns[2]
        detail = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                                capture_output=True, text=True, timeout=15)
        if detail.returncode:
            continue
        paths = [line.strip().removeprefix("path = ") for line in detail.stdout.splitlines()
                 if line.strip().startswith("path = ")]
        if len(paths) != 1:
            continue
        plist = Path(paths[0])
        if plist.name != f"{label}.plist" or plist.exists():
            continue
        try:
            relative = plist.relative_to(base)
        except ValueError:
            continue
        if (len(relative.parts) < 4 or not relative.parts[0].startswith("r-")
                or not relative.parts[1].startswith("pytest-of-")
                or not relative.parts[2].startswith("pytest-")):
            continue
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                       capture_output=True, timeout=15)


def sweep_prior_sessions(request) -> None:
    """Reap dead registered workers and jobs with vanished pytest temp trees."""
    base = Path(request.config._tmp_path_factory.getbasetemp()).parent.parent.parent
    _sweep_missing_plists(base)
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
