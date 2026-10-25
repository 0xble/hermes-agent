"""Scratch-dir retention: idle detection plus the reaping an idle tree needs before it goes.

Deleting an idle ``cache/scratch/<entry>`` is not enough on its own: a lane's e2e run leaves
headless browsers whose cwd was inside the tree (they survived for days with a ``(deleted)``
cwd), and a repo whose linked worktree lived in the tree keeps a dangling registration until
someone runs ``git worktree prune``. Both are reaped here, right before ``rmtree``.

A process Hermes itself started is never an orphan, though: an idle entry a registered live
process still works inside is kept, not reaped (#132401). Ownership comes from the two
registries Hermes writes, not from a marker file a lane must remember to drop.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import stat
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import hermes_constants_scratch_stuck as stuck

logger = logging.getLogger(__name__)

# How long a TERMed process gets before KILL; browsers exit well within this.
_REAP_GRACE_SECONDS = 3.0
# ``.git`` files (linked worktrees) are looked for this deep; lanes nest repo/tree/subtree.
_GIT_FILE_MAX_DEPTH = 4
# logs/scratch-prune.log: one line per entry deleted, per process signalled, and per
# entry rescued or vanished mid-prune (#132401). Same fixed rotation as tool_calls.log.
_PRUNE_LOG_MAX_BYTES = 5 * 1024 * 1024
_PRUNE_LOG_BACKUPS = 3
# Retention declarations, shared with the host storage cleaner (maintain-macos-storage):
# a ``.keep`` anywhere in an entry pins it, and an entry's top-level ``input-pins.json``
# (``{"files": {"<absolute path>": {...}}}``) pins that entry plus every scratch entry holding
# one of its declared paths, transitively. A declaration lives until its owner deletes it.
_KEEP_MARKER = ".keep"
_INPUT_PINS = "input-pins.json"
# Package fixtures under these trees are not user keep markers.
_KEEP_SCAN_SKIP = frozenset({"node_modules", ".venv", "venv"})


class UnreadableRetention(Exception):
    """A retention declaration exists but cannot be read, so its dependencies are unknown."""


# Paths and process names go in with %r: a control character (a newline in a legal POSIX name)
# is written escaped, so it cannot split a record or forge one.


class _PruneLogHandler(RotatingFileHandler):
    """Rollover that tolerates a concurrent prune of the same home rotating first: its rename
    of ``scratch-prune.log`` can land between this handler's existence check and its own
    rename. The rotation is then already done, so the record goes to the new file instead of
    being dropped with a FileNotFoundError."""

    def doRollover(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            super().doRollover()


def _open_prune_log(log_file: Path | None) -> logging.Logger:
    """Logger for one prune's records, with its own RotatingFileHandler on *log_file*, so a
    prune that runs at boot (before ``setup_logging()``) still leaves them on disk. Records also
    propagate through this module's logger to whatever logging is set up.

    One unregistered logger per prune, closed when it ends: no process holds the file open
    between prunes (an open handle blocks rollover on Windows), and prunes of two homes never
    share a handler. Without a *log_file* (test-runner temp roots) this module's logger is used.
    """
    if log_file is None:
        return logger
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("scratch prune: cannot create %s: %s", log_file.parent, exc)
        return logger
    audit = logging.Logger(logger.name, logging.INFO)
    audit.parent = logger
    # backslashreplace: a legal POSIX name that is not UTF-8 is written escaped instead of
    # dropping the whole record.
    handler = _PruneLogHandler(
        log_file, maxBytes=_PRUNE_LOG_MAX_BYTES, backupCount=_PRUNE_LOG_BACKUPS, encoding="utf-8",
        errors="backslashreplace", delay=True,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    audit.addHandler(handler)
    return audit


def _tree_bytes(path: Path) -> int:
    """Apparent size of *path* and everything under it, symlinks not followed."""
    try:
        total = os.lstat(path).st_size
    except OSError:
        return 0
    stack = [str(path)] if path.is_dir() and not path.is_symlink() else []
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for child in it:
                    try:
                        total += child.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if child.is_dir(follow_symlinks=False):
                        stack.append(child.path)
        except OSError:
            continue
    return total


_TOUCHED = "touched"
_SCAN_FAILED = "scan-failed"
_IDLE = "idle"


def subtree_touch_state(path: Path, cutoff: float) -> str:
    """Tri-state form of :func:`subtree_touched_since`: ``touched`` (recent write seen),
    ``scan-failed`` (unreadable — idle cannot be established, so the entry is kept, but
    a failed scan is not activity and is never reported as such), or ``idle``.
    Same walk, same stop-at-first-recent shortcut, same symlink rules. A hardlinked file
    (``st_nlink > 1``) does not count either: its mtime is the inode's, and
    ``git clone --local`` hardlinks packs that git freshens from the source repo."""
    return _scan_subtree(path, cutoff)[0]


def _scan_subtree(path: Path, cutoff: float) -> tuple[str, str | None]:
    """``(state, denied_dir)``: *denied_dir* is the directory below *path* that refused
    listing with EACCES/EPERM when that is why the scan failed (else None)."""
    try:
        if os.lstat(path).st_mtime >= cutoff:
            return _TOUCHED, None
        if not path.is_dir() or path.is_symlink():
            return _IDLE, None
    except OSError:
        return _SCAN_FAILED, None
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for child in it:
                    try:
                        st = child.stat(follow_symlinks=False)
                    except OSError as exc:
                        return _SCAN_FAILED, current if stuck.is_permission_error(exc) else None
                    if st.st_mtime >= cutoff and not (st.st_nlink > 1 and child.is_file(follow_symlinks=False)):
                        return _TOUCHED, None
                    if child.is_dir(follow_symlinks=False):
                        stack.append(child.path)
        except OSError as exc:
            return _SCAN_FAILED, current if stuck.is_permission_error(exc) else None
    return _IDLE, None


def subtree_touched_since(path: Path, cutoff: float) -> bool:
    """True when *path* or anything beneath it has an mtime at or after *cutoff*.

    Stops at the first recent entry, so a live tree costs one hit and only a truly idle
    tree pays for the full walk (once, right before it is deleted). Symlinks are never
    followed: a link into the repo would make the target's activity keep the entry alive.
    An unreadable entry is kept: an incomplete scan cannot establish that it is idle —
    so True here means "touched **or** uninspectable", and callers that need to tell the
    two apart (the mid-prune audit) use :func:`subtree_touch_state` instead.
    """
    return subtree_touch_state(path, cutoff) != _IDLE


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _has_keep_marker(entry: Path) -> bool:
    """A ``.keep`` file or directory anywhere in *entry*, symlinks not followed. An unreadable
    directory counts as marked: an incomplete scan cannot rule a marker out."""
    if entry.name == _KEEP_MARKER:
        return True
    try:
        if not entry.is_dir() or entry.is_symlink():
            return False
    except OSError:
        return True
    stack = [str(entry)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for child in it:
                    if child.name == _KEEP_MARKER:
                        return True
                    if child.is_dir(follow_symlinks=False) and child.name not in _KEEP_SCAN_SKIP:
                        stack.append(child.path)
        except OSError:
            return True
    return False


def _declared_inputs(entry: Path) -> list[str] | None:
    """Absolute paths *entry*'s ``input-pins.json`` declares, or None without a declaration.
    Raises :class:`UnreadableRetention` when the declaration may exist but cannot be read. An
    explicit ``stat`` separates "absent" from "could not look": ``Path.is_file`` reports a
    permission error as absence, which would let an unreadable owner's inputs be pruned."""
    if entry.is_symlink():
        return None
    pins = entry / _INPUT_PINS
    try:
        st = os.stat(pins)
    except (FileNotFoundError, NotADirectoryError):
        # A dangling ``input-pins.json`` link is a declaration whose contents are gone, not an
        # absent one: only a name that does not exist at all means "nothing declared".
        if os.path.lexists(pins):
            raise UnreadableRetention(f"{pins}: declaration target is missing") from None
        return None
    except OSError as exc:
        raise UnreadableRetention(f"{pins}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise UnreadableRetention(f"{pins}: not a regular file")
    try:
        data = json.loads(pins.read_text(encoding="utf-8"))
        files = data["files"]
        if not isinstance(files, dict) or not all(isinstance(name, str) for name in files):
            raise TypeError("files must map path strings")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UnreadableRetention(f"{pins}: {exc}") from exc
    return list(files)


_SYMLINK_HOPS = 40


def _identity(path: str) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of *path* itself (links not followed), or None when it is gone."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _prefixes(path: str) -> list[str]:
    """Every ancestor-or-self prefix of an already-resolved absolute *path*."""
    parts = Path(path).parts
    out, cur = [], parts[0]
    for part in parts[1:]:
        cur = os.path.join(cur, part)
        out.append(cur)
    return out


def _entries_on_path(path: str, by_identity: dict[tuple[int, int], set[str]]) -> set[str]:
    """Top-level scratch entry names a declared *path* passes through on its way to the data,
    including every symlink hop and the entry holding its realpath. Components are matched to
    entries by filesystem identity, not spelling: on a case-insensitive volume a declaration may
    name ``Input`` as ``input``, or spell the scratch root differently, and a string comparison
    would let the pinned entry be pruned."""
    tops: set[str] = set()

    def note(component: str) -> None:
        ident = _identity(component)
        if ident is not None:
            tops.update(by_identity.get(ident, ()))

    # Join, never normpath: ``alias/../f`` must traverse ``alias`` before ``..`` applies, and
    # ``abspath`` would drop it.
    start = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    pending = [start]
    seen: set[str] = set()
    while pending and len(seen) < _SYMLINK_HOPS:
        candidate = pending.pop()
        if candidate in seen:
            continue
        seen.add(candidate)
        parts = Path(candidate).parts
        cur = parts[0]
        for i, part in enumerate(parts[1:], 1):
            if part == ".":
                continue
            # Every earlier component is a real directory (a symlink ends this walk), so ``..``
            # here is the physical parent.
            cur = os.path.dirname(cur) if part == ".." else os.path.join(cur, part)
            note(cur)
            if os.path.islink(cur):
                try:
                    target = os.readlink(cur)
                except OSError:
                    break
                pending.append(os.path.join(os.path.dirname(cur), target, *parts[i + 1:]))
                break
    for component in _prefixes(os.path.realpath(start)):
        note(component)
    return tops


def retained_entries(root: Path, entries: list[Path], candidates: list[Path]) -> set[str]:
    """Names of *candidates* that a retention declaration holds. A candidate is held when it has a
    ``.keep``, declares input pins itself, or contains an input declared by any entry of *root*,
    followed transitively through that dependency's own declaration. Each declared path, and the
    declaration file's own path, holds every top-level entry it passes through, symlink hops
    included, and the entry holding its realpath, so aliases, directory pins and a linked
    ``input-pins.json`` keep both the link and what it points at.

    *entries* is every top-level entry (declarations anywhere protect a candidate); only
    *candidates* pay for the ``.keep`` walk, since a marker protects only its own entry.

    Raises :class:`UnreadableRetention` when any declaration cannot be read: its dependencies are
    unknown, so no candidate can be shown to be free of them.
    """
    by_identity: dict[tuple[int, int], set[str]] = {}
    for entry in entries:
        ident = _identity(str(entry))
        if ident is not None:
            by_identity.setdefault(ident, set()).add(entry.name)
    declared: dict[str, list[str]] = {}
    for entry in entries:
        paths = _declared_inputs(entry)
        if paths is not None:
            declared[entry.name] = [str(entry / _INPUT_PINS), *paths]
    reached: set[str] = set()
    pending = list(declared)
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        for path in declared.get(name, ()):
            pending.extend(top for top in _entries_on_path(path, by_identity) if top not in reached)
    return {c.name for c in candidates if c.name in reached or _has_keep_marker(c)}


def _retention_filter(root: Path, skip_names: frozenset[str], doomed: list[Path],
                      audit: logging.Logger) -> list[Path]:
    """*doomed* minus every entry a retention declaration holds, read fresh from *root*. An
    unreadable declaration keeps every candidate: its dependencies cannot be ruled out."""
    if not doomed:
        return doomed
    try:
        entries = [e for e in root.iterdir() if e.name not in skip_names]
        held = retained_entries(root, entries, doomed)
    except UnreadableRetention as exc:
        audit.info("scratch prune: kept all %d idle entries, unreadable retention declaration: %r",
                   len(doomed), str(exc))
        return []
    except OSError as exc:
        audit.info("scratch prune: kept all %d idle entries, retention scan failed: %r", len(doomed),
                   str(exc))
        return []
    for entry in doomed:
        if entry.name in held:
            audit.info("scratch prune: kept %r, held by a retention declaration", os.fspath(entry))
    return [e for e in doomed if e.name not in held]


def _own_lineage() -> set[int]:
    """This process and its ancestors: never reap the shell that is running the prune."""
    import psutil

    pids: set[int] = set()
    try:
        proc = psutil.Process()
        while proc is not None and proc.pid not in pids:
            pids.add(proc.pid)
            proc = proc.parent()
    except (psutil.Error, OSError):
        pass
    return pids


def _registered_live_pids(home: Path) -> set[int]:
    """Live pids Hermes registered for *home*: the machine spawn ledger (every long-lived entry
    point and MCP helper, ``hermes_cli.process_identity``) and the background-process checkpoint
    (``tools.process_registry``, ``processes.json``). A recorded start time that no longer
    matches is a reused pid, not ownership."""
    import psutil

    from hermes_cli.process_identity import spawner_is_dead
    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root(home=home)
    sources = [(root / "spawn-ledger.json", "create_time")]
    sources += [(base / "processes.json", "host_start_time") for base in {home, root}]
    pids: set[int] = set()
    for path, stamp_key in sources:
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"process ownership registry is not a list: {path}")
        for entry in entries:
            pid, stamp = (entry.get("pid"), entry.get(stamp_key)) if isinstance(entry, dict) else (None, None)
            if not isinstance(pid, int) or pid <= 0 or entry.get("pid_scope", "host") != "host":
                continue
            # An MCP helper whose recorded spawner is provably gone is an orphan, not owned: the
            # rule reap_orphaned_mcp_helpers applies. Long-lived entry points (gateway, serve) that
            # outlive their launcher stay owned; an unprovable spawner keeps ownership.
            if entry.get("purpose") == "mcp-helper" and spawner_is_dead(entry) is True:
                continue
            try:
                if stamp is None:
                    alive = psutil.pid_exists(pid)
                elif stamp_key == "create_time":
                    alive = abs(psutil.Process(pid).create_time() - float(stamp)) < 2.0
                else:
                    from gateway.status import get_process_start_time

                    alive = get_process_start_time(pid) == stamp
            except (psutil.Error, OSError, TypeError, ValueError):
                continue
            if alive:
                pids.add(pid)
    return pids


def _owned_by_hermes(proc, registered: set[int]) -> bool:
    """Registered itself, or a descendant of a live registered process (a background shell's
    child). An orphan whose spawning shell is gone was reparented away from that lineage, so
    it is not owned and is still reaped."""
    import psutil

    try:
        # pid 1 is excluded as an ancestor: a Hermes running as a container init adopts every orphan.
        return proc.pid in registered or any(p.pid in registered and p.pid != 1 for p in proc.parents())
    except (psutil.Error, OSError):
        return False


def reap_processes_rooted_in(scratch_root: Path, doomed: list[Path], audit: logging.Logger = logger) -> int:
    """TERM (then KILL) same-user processes whose cwd is inside an entry about to be
    pruned, or is a path under *scratch_root* that no longer exists. Returns the count.

    Only the cwd is consulted: a process merely holding a file open inside scratch (an
    editor, a log tail) is not ours to kill, but one *living* in a directory we are
    about to delete, or in one already gone, has nothing left to run for.

    A process Hermes owns (:func:`_owned_by_hermes`) is never signalled; each doomed entry it
    works inside is dropped from *doomed* in place and recorded as kept.
    """
    import psutil

    root = os.path.realpath(str(scratch_root))
    targets = [os.path.realpath(str(p)) for p in doomed]
    skip = _own_lineage()
    uid = os.getuid() if hasattr(os, "getuid") else None
    victims: list[tuple[psutil.Process, str]] = []
    # The pruner's own lineage is never signalled, but its cwd still keeps an entry: a live
    # session that launched this Hermes command may be working inside it.
    lineage: list[tuple[psutil.Process, str]] = []
    for proc in psutil.process_iter(["pid"]):
        try:
            if uid is not None and proc.uids().real != uid:
                continue
            cwd = proc.cwd()
        except (psutil.Error, OSError):
            continue
        if not cwd:
            continue
        deleted = cwd.endswith(" (deleted)")
        cwd_path = cwd[: -len(" (deleted)")] if deleted else cwd
        if not _under(cwd_path, root):
            continue
        if deleted or not os.path.exists(cwd_path) or any(_under(cwd_path, t) for t in targets):
            (lineage if proc.pid in skip else victims).append((proc, cwd_path))
    # The owning home comes from the scratch path as configured, not its resolved target:
    # a cache/scratch symlinked onto another disk still belongs to <home>.
    registered = _registered_live_pids(Path(scratch_root).absolute().parent.parent) if victims else set()
    owned = lineage + [(proc, cwd_path) for proc, cwd_path in victims if _owned_by_hermes(proc, registered)]
    for proc, cwd_path in owned:
        for entry in [e for e in doomed if _under(cwd_path, os.path.realpath(str(e)))]:
            doomed.remove(entry)
            try:
                name = proc.name()
            except (psutil.Error, OSError):
                name = "?"
            audit.info("scratch prune: kept %r — live Hermes process pid=%d (%r) works inside",
                       os.fspath(entry), proc.pid, name)
    # Re-filter against the shrunk *doomed*: an orphan in a kept entry is spared with it (the
    # reap only exists to clear the way for rmtree); one whose cwd is already gone still goes.
    victims = [(proc, cwd_path) for proc, cwd_path in victims if (proc, cwd_path) not in owned
               and (not os.path.exists(cwd_path) or any(_under(cwd_path, os.path.realpath(str(e))) for e in doomed))]
    if not victims:
        return 0
    for proc, cwd_path in victims:
        try:
            name = proc.name()
            proc.terminate()
        except (psutil.Error, OSError):
            continue
        audit.info("scratch prune: sent TERM pid=%d name=%r cwd=%r", proc.pid, name, cwd_path)
    _, alive = psutil.wait_procs([proc for proc, _ in victims], timeout=_REAP_GRACE_SECONDS)
    for proc in alive:
        try:
            # A zombie already exited; only its parent has not collected it yet.
            if proc.status() == psutil.STATUS_ZOMBIE:
                continue
            proc.kill()
        except (psutil.Error, OSError):
            continue
        audit.info("scratch prune: sent KILL pid=%d (still running after TERM)", proc.pid)
    logger.info("scratch prune: reaped %d process(es) rooted in pruned entries", len(victims))
    return len(victims)


def _linked_worktree_repos(entry: Path) -> set[str]:
    """Repos whose linked worktrees live inside *entry* (``.git`` FILES, ``gitdir: <repo>/.git/worktrees/<n>``)."""
    repos: set[str] = set()
    stack = [(str(entry), 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                for child in it:
                    if child.name == ".git" and child.is_file(follow_symlinks=False):
                        try:
                            line = Path(child.path).read_text(encoding="utf-8-sig", errors="replace").strip()
                        except OSError:
                            continue
                        if line.startswith("gitdir:"):
                            gitdir = Path(line[len("gitdir:"):].strip())
                            # <repo>/.git/worktrees/<name> -> <repo>
                            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                                repos.add(str(gitdir.parent.parent.parent))
                    elif child.is_dir(follow_symlinks=False) and depth < _GIT_FILE_MAX_DEPTH \
                            and child.name not in ("node_modules", ".venv", "venv"):
                        stack.append((child.path, depth + 1))
        except OSError:
            continue
    return repos


def release_git_worktrees(repos: set[str]) -> None:
    """``git worktree prune`` in each repo: drops registrations whose tree we just deleted."""
    for repo in sorted(repos):
        if not os.path.isdir(repo):
            continue
        try:
            subprocess.run(
                ["git", "-C", repo, "worktree", "prune"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("git worktree prune in %s failed: %s", repo, exc)


def prune_idle_entries(
    root: Path, max_idle_hours: float, skip_names: frozenset[str], log_file: Path | None = None,
) -> int:
    """Delete top-level entries of *root* with no write anywhere in their subtree for
    *max_idle_hours*, reaping processes and worktree registrations rooted in them first.
    Each removal, each signalled process and each entry rescued mid-prune (touched
    again after selection) is recorded in *log_file*. Returns the count actually
    removed."""
    audit = _open_prune_log(log_file)
    try:
        return _prune_idle_entries(root, max_idle_hours, skip_names, audit)
    finally:
        if audit is not logger:
            for handler in audit.handlers:
                handler.close()


def _prune_idle_entries(
    root: Path, max_idle_hours: float, skip_names: frozenset[str], audit: logging.Logger,
) -> int:
    cutoff = time.time() - max_idle_hours * 3600
    # Entries already known to hold files this user cannot read or delete are skipped before
    # any walk, until that is fixed (hermes_constants_scratch_stuck).
    skip_names = skip_names | stuck.load_skips(root) | {stuck.STUCK_STATE_NAME, stuck.STUCK_LOCK_NAME}
    try:
        entries = [e for e in root.iterdir() if e.name not in skip_names]
    except OSError:
        return 0
    doomed = []
    for e in entries:
        state, denied_dir = _scan_subtree(e, cutoff)
        if state == _IDLE:
            doomed.append(e)
        elif denied_dir is not None and stuck.tracking_supported():
            # A dir this user may not list (root-owned 0700) fails every idle scan: kept
            # (fail-closed) as before, but reported once and no longer re-scanned hourly.
            stuck.record(root, e, [denied_dir], audit)
    # Idle is not unused: a declared input of a retained payload may sit untouched for days.
    doomed = _retention_filter(root, skip_names, doomed, audit)
    # Runs even with nothing to delete: orphans whose cwd was removed by an earlier pass
    # (or by hand) are found by the deleted-cwd rule, not by membership in ``doomed``.
    try:
        reap_processes_rooted_in(root, doomed, audit)
    except Exception as exc:
        audit.warning("scratch prune: cleanup deferred, process inspection/reaping failed: %s", exc)
        return 0
    if not doomed:
        return 0
    # Re-read declarations after the reap and before any removal: one written meanwhile still holds.
    doomed = _retention_filter(root, skip_names, doomed, audit)
    repos: set[str] = set()
    removed = 0
    for entry in doomed:
        # Worktree scan first: a registration whose tree an earlier pass (or hand)
        # removed is stale even in a rescued entry, and the re-check below sits as
        # close to the delete as the loop allows.
        if entry.is_dir() and not entry.is_symlink():
            repos |= _linked_worktree_repos(entry)
        # Last-moment re-validation (#132401 C1/F1): ``doomed`` is a snapshot from
        # before the reap ran, and a writer whose cwd is outside scratch — invisible
        # to the reap — can land fresh work in that window. The snapshot is a
        # candidate list, never a verdict: anything touched since selection is
        # rescued here, not deleted. A re-scan that cannot read the entry is also
        # fail-closed (the entry is kept), but a failed scan is not activity — the
        # audit names the failed re-scan instead of inventing a touch that did not
        # happen, so operators can tell an uninspectable candidate from verified
        # recent activity (P3 on #134173).
        # Size is collected before the re-check, not after it: a full-tree walk between
        # the re-check and rmtree reopens the window for a write landing during that
        # walk to be destroyed (review F6 on #139850).
        size = _tree_bytes(entry)
        state = subtree_touch_state(entry, cutoff)
        if state != _IDLE:
            # Only a missing path is a vanish: a stat that raises PermissionError means
            # the entry is there but unreadable, and reporting it as vanished would be
            # the same invented reason the audit exists to prevent.
            try:
                os.lstat(entry)
                entry_gone = False
            except FileNotFoundError:
                entry_gone = True
            except OSError:
                entry_gone = False
            if entry_gone:
                audit.info("scratch prune: entry %r vanished since selection", os.fspath(entry))
            elif state == _TOUCHED:
                audit.info(
                    "scratch prune: rescued %r — touched since selection, kept",
                    os.fspath(entry),
                )
            else:
                audit.info(
                    "scratch prune: kept %r — activity re-scan failed, left untouched",
                    os.fspath(entry),
                )
            continue
        denied: list[str] = []
        try:
            if entry.is_dir() and not entry.is_symlink():
                denied = stuck.rmtree_collecting_permission_errors(entry)
            else:
                entry.unlink()
        except OSError as exc:
            audit.info("scratch prune: could not remove %r: %s", os.fspath(entry), exc)
            continue
        # rmtree ignores errors, so only a path that is really gone counts as removed.
        # Only an explicit lstat proves absence: lexists() swallows OSError from lstat,
        # so an entry that stayed (rmtree left residue) but cannot be inspected would be
        # counted and recorded as removed — the same invented reason the vanish rule
        # above refuses to invent (P2 on #134173).
        try:
            os.lstat(entry)
            if denied and stuck.tracking_supported():
                stuck.record(root, entry, denied, audit)
            else:
                audit.info("scratch prune: could not fully remove %r", os.fspath(entry))
            continue
        except FileNotFoundError:
            pass
        except OSError:
            audit.info("scratch prune: could not confirm removal of %r", os.fspath(entry))
            continue
        audit.info("scratch prune: removed %r (%d bytes)", os.fspath(entry), size)
        removed += 1
    release_git_worktrees(repos)
    return removed
