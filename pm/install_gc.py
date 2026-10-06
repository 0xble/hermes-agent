"""Collection of dependency-install directories whose checkout no longer exists."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import time
from typing import Iterable

from pm.environments import (
    INSTALL_METADATA_FILENAME,
    INSTALL_METADATA_SCHEMA,
    install_key,
    installs_root,
)

# A deleted review checkout should survive a few days of recovery/retry activity.
ORPHAN_INSTALL_GRACE_SECONDS = 7 * 24 * 60 * 60
# Installations created before the sidecar was introduced have no trustworthy last-use
# timestamp. Keep them for a month unless a collector can prove their key belongs to a
# checkout it can see; this is deliberately much longer than the normal grace period.
LEGACY_INSTALL_GRACE_SECONDS = 30 * 24 * 60 * 60


def _record(state: Path) -> tuple[Path, float] | None:
    metadata = state / INSTALL_METADATA_FILENAME
    try:
        data = json.loads(metadata.read_text(encoding="utf-8-sig"))
        value = data.get("project_root")
        if data.get("schema") != INSTALL_METADATA_SCHEMA or not isinstance(value, str):
            return None
        root = Path(value)
        if not root.is_absolute() or install_key(root) != state.name:
            return None
        return root.resolve(), metadata.stat().st_mtime
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def _tree_touched_since(path: Path, cutoff: float) -> bool:
    """Fail closed when any file below *path* is recent or unreadable."""
    try:
        if not path.is_dir() or path.is_symlink():
            return False
    except OSError:
        return True
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.name == ".install.lock":
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                            return True
                    except OSError:
                        return True
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except OSError:
            return True
    return False


@contextmanager
def _install_lock(state: Path):
    """Yield whether the install lock was acquired without waiting."""
    from pm.filesystem import lock_fd

    try:
        fd = os.open(state / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield False
        return
    try:
        yield lock_fd(fd, wait=False)
    finally:
        os.close(fd)


def _leases_held(state: Path) -> bool:
    from hermes_cli.runtime_state import leases_held

    for generations in (state / "environments", state / "pm-runtime" / "generations"):
        try:
            entries = tuple(generations.iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            return True
        for generation in entries:
            if generation.is_dir() and not generation.is_symlink():
                try:
                    if leases_held(generation):
                        return True
                except OSError:
                    return True
    return False


def collect_install_orphans(
    project_roots: Iterable[Path] = (),
    *,
    grace_seconds: float = ORPHAN_INSTALL_GRACE_SECONDS,
    legacy_grace_seconds: float = LEGACY_INSTALL_GRACE_SECONDS,
    now: float | None = None,
) -> list[Path]:
    """Remove idle install directories whose recorded checkout is gone.

    The candidate is rechecked while holding its per-install lock. Active generation
    leases fence deletion even after the checkout directory itself has disappeared.
    Legacy directories without metadata are retained when their key matches a root
    visible to this collector, and otherwise require the longer legacy grace period.
    """
    root = installs_root()
    if not root.is_dir() or root.is_symlink():
        return []
    now = time.time() if now is None else now
    try:
        states = tuple(path for path in root.iterdir()
                       if path.is_dir() and not path.is_symlink() and len(path.name) == 16
                       and all(char in "0123456789abcdef" for char in path.name))
    except OSError:
        return []

    records = {state: _record(state) for state in states}
    known_keys = {install_key(Path(project_root)) for project_root in project_roots}
    known_keys.update(
        install_key(record[0])
        for record in records.values()
        if record is not None and record[0].is_dir()
    )

    candidates: list[Path] = []
    for state, record in records.items():
        if record is not None:
            project_root, last_used = record
            if project_root.is_dir() or now - last_used < grace_seconds:
                continue
        else:
            if state.name in known_keys or _tree_touched_since(
                    state, now - legacy_grace_seconds):
                continue
        candidates.append(state)

    removed: list[Path] = []
    for state in candidates:
        try:
            with _install_lock(state) as held:
                if not held:
                    continue
                current = _record(state)
                if current is not None:
                    project_root, last_used = current
                    if project_root.is_dir() or now - last_used < grace_seconds:
                        continue
                elif state.name in known_keys or _tree_touched_since(
                        state, now - legacy_grace_seconds):
                    continue
                if _leases_held(state):
                    continue
                if state.exists() and not state.is_symlink():
                    shutil.rmtree(state)
                    removed.append(state)
        except (OSError, ValueError):
            # Maintenance is best effort; an install that changes under us is kept.
            continue
    return removed
