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
    install_recovery_lock_path,
    install_state_lock,
    installs_root,
)

_INVALID_RECORD = object()

# Agents create and delete a checkout (worktree) per task, each with a full install, so orphans
# arrive by the hundred per day. Their checkout is gone; the worst case of collecting one is a
# single rebuild if the same path comes back, so a day since its last use is enough.
ORPHAN_INSTALL_GRACE_SECONDS = 24 * 60 * 60
# Installations created before the sidecar was introduced have no trustworthy last-use
# timestamp, so they are judged by tree activity instead. They are regenerable dependency
# caches (the worst case of an early removal is one rebuild), so a few idle days is enough
# once their key matches no checkout this collector can see. The install lock, the
# generation-lease fence and the known-key check still apply exactly as for recorded installs.
LEGACY_INSTALL_GRACE_SECONDS = 3 * 24 * 60 * 60
# A recorded install holding a generation created before leases existed cannot prove it is
# unused: a reader may hold it without a lease. That case keeps the long, month-scale window.
UNLEASEABLE_INSTALL_GRACE_SECONDS = 30 * 24 * 60 * 60
# An install is renamed to this prefix (outside the 16-hex key space) before it is deleted.
_CONDEMNED_PREFIX = ".gc-"


def _record(state: Path):
    metadata = state / INSTALL_METADATA_FILENAME
    try:
        text = metadata.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError:
        return _INVALID_RECORD
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return _INVALID_RECORD
        value = data.get("project_root")
        if data.get("schema") != INSTALL_METADATA_SCHEMA or not isinstance(value, str):
            return _INVALID_RECORD
        root = Path(value)
        if not root.is_absolute() or install_key(root) != state.name:
            return _INVALID_RECORD
        return root.resolve(), metadata.stat().st_mtime
    except (OSError, ValueError, TypeError):
        return _INVALID_RECORD


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
    """Yield whether the install-state and recovery locks were both acquired without waiting."""
    from pm.filesystem import lock_fd

    with install_state_lock(state, timeout=0) as held:
        if not held:
            yield False
            return
        recovery = install_recovery_lock_path(state)
        fd = os.open(recovery, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            yield lock_fd(fd, wait=False)
        finally:
            os.close(fd)


def _generations(state: Path) -> list[Path] | None:
    """Generation directories below *state*, or None when they cannot be listed."""
    found: list[Path] = []
    for generations in (state / "environments", state / "pm-runtime" / "generations"):
        try:
            entries = tuple(generations.iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            return None
        found.extend(generation for generation in entries
                     if generation.is_dir() and not generation.is_symlink())
    return found


def _leases_held(state: Path) -> bool:
    from hermes_cli.runtime_state import leases_held

    generations = _generations(state)
    if generations is None:
        return True
    for generation in generations:
        try:
            if leases_held(generation):
                return True
        except OSError:
            return True
    return False


def _has_unleaseable_generation(state: Path) -> bool:
    """True when a pre-lease generation exists; its readers cannot be observed.

    ``lease_directory`` hands such generations a no-op lease, so an empty lease
    directory proves nothing about them. Fail closed when listing fails.
    """
    generations = _generations(state)
    if generations is None:
        return True
    try:
        return any(not (generation / ".lease-managed").is_file() for generation in generations)
    except OSError:
        return True


def _eligible(state: Path, record, known_keys: set[str],
              now: float, grace_seconds: float, legacy_grace_seconds: float,
              unleaseable_grace_seconds: float = UNLEASEABLE_INSTALL_GRACE_SECONDS) -> bool:
    if record is _INVALID_RECORD:
        return False
    if record is not None:
        project_root, last_used = record
        if project_root.is_dir() or now - last_used < grace_seconds:
            return False
        # A pre-lease generation cannot prove no reader holds it: keep the long window.
        if _has_unleaseable_generation(state):
            return not _tree_touched_since(state, now - unleaseable_grace_seconds)
        return True
    return state.name not in known_keys and not _tree_touched_since(
        state, now - legacy_grace_seconds)


def _remove_condemned(path: Path) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def collect_install_orphans(
    project_roots: Iterable[Path] = (),
    *,
    grace_seconds: float = ORPHAN_INSTALL_GRACE_SECONDS,
    legacy_grace_seconds: float = LEGACY_INSTALL_GRACE_SECONDS,
    now: float | None = None,
    limit: int | None = None,
) -> list[Path]:
    """Remove idle install directories whose recorded checkout is gone.

    *limit* bounds how many are removed in one call: each holds tens of thousands of files, and
    deleting a large backlog at once saturates the filesystem on a nearly full volume.

    The candidate is rechecked while holding its per-install lock. Active generation
    leases fence deletion even after the checkout directory itself has disappeared.
    Legacy directories without metadata are retained when their key matches a root
    visible to this collector, and otherwise after a few idle days (they are caches).
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
        if isinstance(record, tuple) and record[0].is_dir()
    )

    removed: list[Path] = []

    def full() -> bool:
        return limit is not None and len(removed) >= limit

    # A pass killed mid-delete (gateway exit) leaves a condemned tree: finish it first.
    for condemned in sorted(root.glob(f"{_CONDEMNED_PREFIX}*")):
        if full():
            return removed
        if _remove_condemned(condemned):
            removed.append(condemned)

    # Lazily, recorded installs first: judging a legacy install walks its whole tree, and a
    # limited pass should not pay that for candidates it will never reach.
    for state in sorted(states, key=lambda state: records[state] is None):
        if full():
            break
        if not _eligible(state, records[state], known_keys, now, grace_seconds, legacy_grace_seconds):
            continue
        try:
            with _install_lock(state) as held:
                if not held:
                    continue
                if not _eligible(state, _record(state), known_keys, now,
                                 grace_seconds, legacy_grace_seconds):
                    continue
                if _leases_held(state):
                    continue
                if not state.exists() or state.is_symlink():
                    continue
                # One atomic rename condemns the whole install, so an interrupted delete can never
                # leave a recordless or lease-less remnant that looks live or legacy.
                condemned = root / f"{_CONDEMNED_PREFIX}{state.name}-{os.getpid()}"
                state.rename(condemned)
        except (OSError, ValueError):
            # Maintenance is best effort; an install that changes under us is kept.
            continue
        _remove_condemned(condemned)
        removed.append(state)
    return removed
