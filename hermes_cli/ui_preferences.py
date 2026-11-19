"""Machine-level Desktop/Webapp UI preferences (#145213).

One JSON document per machine, beside the ROOT Hermes home (``get_default_hermes_root()``, never a
profile's ``HERMES_HOME``): native Desktop writes it through its preload capability
(``apps/desktop/electron/ui-preferences.ts``) and every browser that opens this machine's Webapp
writes it through ``/api/ui-preferences``. It is deliberately neither per profile nor per gateway
connection; a key that is scoped (``….profile.<name>``) carries its scope in its own name.

Both writers share the on-disk contract, so keep the two in lockstep:

* document ``{"version": 1, "revision": <int>, "values": {<key>: <string>}}``;
* a write merges per key under the ``<store>.lock`` directory lock (``mkdir`` is atomic on every
  OS and in Node), bumps ``revision`` and lands by temp file + rename; a ``None`` value deletes;
* ``generation`` names one store's lifetime: a write that finds no readable store (missing, or
  corrupt and kept aside as ``<store>.corrupt-<ms>``) starts a new one, and every window rejoins it
  (re-imports its own values, then follows it) whatever its revision high-water mark was;
* a ``seed`` write (a client's first contact with a generation) only ADDS the keys the store lacks,
  so the client that has data fills a store an empty client created, and nothing is overwritten;
* only a missing file reads as "no store": any other read failure refuses the write
  (:class:`PreferencesBusy`) instead of merging over nothing.

Values are opaque strings the renderer already kept in localStorage. Only keys under the Desktop
prefixes are accepted; the renderer's allowlist decides which of those are preferences and keeps
credentials, grants and per-window state out.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional

from utils import atomic_json_write

STORE_NAME = "ui-preferences.json"
KEY_PREFIXES = ("hermes.desktop.", "hermes-desktop-", "hermes-onboarding-")
MAX_KEY_CHARS = 256
MAX_VALUE_CHARS = 256 * 1024
MAX_KEYS = 512
MAX_TOTAL_CHARS = 2 * 1024 * 1024
_LOCK_STALE_S = 10.0
_LOCK_MAX_AGE_S = 10 * 60.0
_LOCK_WAIT_S = 30.0
_LOCK_POLL_S = 0.02
_monotonic = time.monotonic
_sleep = time.sleep


class PreferencesError(ValueError):
    """A write the store refuses (bad key, value or size); the route answers 400."""


class PreferencesBusy(RuntimeError):
    """The store could not be read safely or a lock was taken over during a write; callers
    should retry instead of replacing unknown data."""


def store_path(root: Optional[Path] = None) -> Path:
    """The machine's preferences file, resolved by the same rule native Desktop applies to its
    ``HERMES_HOME`` (``resolveDesktopHermesHome`` + ``uiPreferencesRoot``): a profile home
    (``<root>/profiles/<name>``, judged on the path as given, so a symlinked profile directory
    still names its root) names its root, any other home is its own root, and that root is then
    resolved through symlinks on both sides. Every profile one process serves maps to the same
    root, so this read is profile-independent by design (not a launch-profile leak)."""
    if root is None:
        from hermes_constants import get_hermes_home

        home = Path(os.path.abspath(get_hermes_home()))
        root = (home.parent.parent if home.parent.name.lower() == "profiles" else home).resolve()
    return Path(root) / STORE_NAME


def _sanitize(values: object) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    return {k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str)}


_MISSING = {"exists": False, "revision": 0, "values": {}}


def _read(path: Path) -> tuple[dict, bool]:
    """``(snapshot, corrupt)``. A missing file is no store; an unparseable one is no store too
    (``corrupt``), so clients re-import into a fresh generation. Any other read failure (EACCES,
    EBUSY, EMFILE…) raises :class:`PreferencesBusy`: it says nothing about the content, and
    merging over it would drop every key."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return dict(_MISSING, values={}), False
    except OSError as exc:
        raise PreferencesBusy(f"preferences store is unreadable: {exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return dict(_MISSING, values={}), True
    revision = doc.get("revision")
    generation = doc.get("generation")
    return {
        "exists": True,
        "revision": revision if isinstance(revision, int) else 0,
        "values": _sanitize(doc.get("values")),
        "generation": generation if isinstance(generation, str) else "",
    }, False


def read_preferences(path: Optional[Path] = None) -> dict:
    """``{"exists", "revision", "values"[, "generation"]}`` for the store at *path* (the
    machine's by default). Raises :class:`PreferencesBusy` when the file exists but cannot be read."""
    return _read(path or store_path())[0]


def _validate(changes: Mapping[str, Optional[str]]) -> None:
    for key, value in changes.items():
        if not isinstance(key, str) or not key.startswith(KEY_PREFIXES) or len(key) > MAX_KEY_CHARS:
            raise PreferencesError(f"unsupported preference key: {key!r}")
        if value is not None and (not isinstance(value, str) or len(value) > MAX_VALUE_CHARS):
            raise PreferencesError(f"unsupported value for {key}")


def _owner_file(lock_dir: Path) -> Path:
    return lock_dir / "owner"


def _release(lock_dir: Path, token: str) -> None:
    """Remove the lock only while it is still ours: a lock broken as stale and re-taken by another
    writer must not be removed by the writer that lost it."""
    if not _owns(lock_dir, token):
        return
    try:
        _owner_file(lock_dir).unlink()
        os.rmdir(lock_dir)
    except OSError:
        pass


def _owns(lock_dir: Path, token: str) -> bool:
    try:
        return _owner_file(lock_dir).read_text(encoding="utf-8-sig") == token
    except OSError:
        return False


def _owner_token(lock_dir: Path) -> Optional[str]:
    try:
        return _owner_file(lock_dir).read_text(encoding="utf-8-sig")
    except OSError:
        return None


def _owner_pid(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    pid_field = raw.split("-", 1)[0]
    if not (pid_field.isascii() and pid_field.isdigit()) or len(pid_field) > 10:
        return None
    pid = int(pid_field)
    return pid if pid > 0 else None


def _pid_is_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    # Keep process probing in the gateway's cross-platform helper: os.kill(pid, 0)
    # sends CTRL_C_EVENT on Windows instead of being a harmless existence check.
    from gateway.status import _pid_exists

    return _pid_exists(pid)


def _owner_age(lock_dir: Path) -> Optional[float]:
    """Age of the owner file, or of the lock directory itself when no owner was written:
    a writer that died between mkdir and the owner write must still age out."""
    for candidate in (_owner_file(lock_dir), lock_dir):
        try:
            return time.time() - candidate.stat().st_mtime
        except FileNotFoundError:
            continue
    return None


def _break_if_stale(lock_dir: Path) -> None:
    """Atomically quarantine a lock whose owner is dead or whose age backstop expired."""
    judged_owner = _owner_token(lock_dir)
    pid = _owner_pid(judged_owner)
    age = _owner_age(lock_dir)
    if _pid_is_alive(pid) and (age is None or age <= _LOCK_MAX_AGE_S):
        return
    if pid is None and (age is None or age <= _LOCK_STALE_S):
        return
    if _owner_token(lock_dir) != judged_owner:
        return

    abandoned = lock_dir.with_name(f"{lock_dir.name}.stale-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        os.rename(lock_dir, abandoned)
    except FileNotFoundError:
        return
    except OSError:
        return

    # The owner token can appear or change between the stale check and rename. Preserve that
    # lock rather than deleting it, then let the acquisition loop poll within its wait budget.
    if _owner_token(abandoned) != judged_owner:
        try:
            os.rename(abandoned, lock_dir)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
        return
    shutil.rmtree(abandoned, ignore_errors=True)


@contextmanager
def _store_lock(path: Path) -> Iterator[Callable[[], bool]]:
    """Cross-process lock shared with the Electron writer: the ``<store>.lock`` directory, whose
    ``owner`` file names the holder."""
    lock_dir = path.with_name(path.name + ".lock")
    token = f"{os.getpid()}-{secrets.token_hex(8)}"
    started = _monotonic()
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            if _monotonic() - started >= _LOCK_WAIT_S:
                raise PreferencesBusy("preferences store is busy")
            _break_if_stale(lock_dir)
            _sleep(_LOCK_POLL_S)
    try:
        _owner_file(lock_dir).write_text(token, encoding="utf-8")
        yield lambda: _owns(lock_dir, token)
    finally:
        _release(lock_dir, token)


def _merge(current: Mapping[str, str], changes: Mapping[str, Optional[str]], seed: bool) -> dict[str, str]:
    values = dict(current)
    for key, value in changes.items():
        if seed:
            # A client's first contact: fill what the store lacks, never overwrite or delete.
            if value is not None:
                values.setdefault(key, value)
        elif value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


def write_preferences(changes: Mapping[str, Optional[str]], *, seed: bool = False,
                      path: Optional[Path] = None) -> dict:
    """Merge *changes* per key (``None`` deletes) and return the resulting snapshot.

    ``seed=True`` is a client's first contact with this store generation: it only adds the keys
    the store lacks (and creates no store from nothing), so a client that has data fills a store
    an empty client created and no client's values are overwritten or deleted.
    """
    _validate(changes)
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _store_lock(path) as still_ours:
        current, corrupt = _read(path)
        values = _merge(current["values"], changes, seed)
        if len(values) > MAX_KEYS or sum(len(k) + len(v) for k, v in values.items()) > MAX_TOTAL_CHARS:
            raise PreferencesError("preferences store is full")
        if values == current["values"] and (current["exists"] or not values):
            return current
        revision = current["revision"] + 1
        generation = current.get("generation") or secrets.token_hex(8)
        if not still_ours():
            # Held past the stale limit and broken by another writer: replacing the file now
            # could drop the keys that writer merged meanwhile.
            raise PreferencesBusy("preferences store lock was taken over")
        if corrupt:
            # Kept aside, never silently replaced: it may hold preferences worth recovering.
            shutil.copy2(path, path.with_name(f"{path.name}.corrupt-{int(time.time() * 1000)}"))
        atomic_json_write(path, {"version": 1, "generation": generation, "revision": revision,
                                 "values": values}, mode=0o600)
    return {"exists": True, "revision": revision, "values": values, "generation": generation}
