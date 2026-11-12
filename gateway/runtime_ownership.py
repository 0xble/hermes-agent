"""Nonblocking, all-or-nothing reservations of canonical profile homes.

A held lock inode is never removed: unlinking it creates a second owner. The one exception is a
stale inode this user cannot write (left by a gateway that ran as root, #42685) that a flock through
a read-only descriptor proves unheld; it is replaced once, and every winner checks that the path
still names the inode it locked.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import stat
import threading
import weakref
from hermes_dir_acl import make_user_dir


class OwnershipConflict(RuntimeError):
    pass


def canonical_home(home: Path) -> Path:
    return Path(os.path.normcase(str(Path(home).expanduser().resolve())))


def tighten_lock_mode(fd: int) -> None:
    """Validate an open ``gateway.lock`` descriptor and narrow an upgraded lock to 0600.

    Main's ``acquire_gateway_runtime_lock`` created the inode with ``open(path, "a+")`` under the
    umask (0644 on most hosts), and lock inodes are never replaced, so an upgraded home keeps that
    mode forever and discovery refuses it. Only a regular, single-link inode this user owns is
    chmodded, through the already-open NOFOLLOW descriptor (no path race); anything else is refused.
    """
    info = os.fstat(fd)
    if os.name == 'nt':
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError('unsafe gateway lock owner or type')
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():  # windows-footgun: ok — POSIX branch
        raise PermissionError('unsafe gateway lock owner or type')
    if stat.S_IMODE(info.st_mode) & 0o077:
        if info.st_nlink != 1:
            raise PermissionError('unsafe gateway lock link count')
        os.fchmod(fd, 0o600)  # windows-footgun: ok — POSIX branch


_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)


def _open_checked(path: Path, flags: int) -> int:
    fd = os.open(path, flags, 0o600)
    try:
        tighten_lock_mode(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _unlink_stale_lock(path: Path) -> bool:
    """Unlink a ``gateway.lock`` this user cannot write (another uid's) once a flock taken through a
    read-only descriptor proves it unheld. Symlinks, hardlinks, non-regular files, a home whose
    ``gateway.pid`` names a live process and anything not provably unheld are left in place (False).
    An inode this user cannot even open is refused with the recovery command: an unheld snapshot
    (``/proc/locks``) does not stop a holder locking it between the read and the unlink."""
    from gateway.status import _live_pid_from_record, _read_pid_record
    if os.name == 'nt':
        return False  # a sharing violation is a live holder there
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return False
    if _live_pid_from_record(_read_pid_record(path.with_name('gateway.pid'))) is not None:
        return False
    try:
        probe = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK)
    except PermissionError:
        raise PermissionError(foreign_lock_recovery(path)) from None
    import fcntl
    try:
        if not _same_inode(os.fstat(probe), info):
            return False
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        # Unlinked while still locked: a late locker of the old inode never shares our lock.
        return _unlink_if_same(path, info)
    finally:
        os.close(probe)


def foreign_lock_recovery(path: Path) -> str:
    return (f'foreign_stale_lock: {path} is not readable by this user (left by a gateway run as root?) '
            f'and cannot be proven unheld. If `sudo lsof {path}` lists no process, run `sudo rm {path}`.')


def unreadable_lock_recovery(path: Path) -> str | None:
    """The recovery a client prints for a ``gateway.lock`` it cannot even open read-only."""
    try:
        os.close(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK))
    except PermissionError:
        return foreign_lock_recovery(path)
    except OSError:
        pass
    return None


def _unlink_if_same(path: Path, info) -> bool:
    if not _same_inode(os.lstat(path), info):
        return False
    os.unlink(path)
    return True


def _same_inode(left, right) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_lock(path: Path) -> int:
    """Descriptor for *path*, replacing a provably stale inode this user cannot use once."""
    try:
        return _open_checked(path, _LOCK_FLAGS)
    except PermissionError:
        if not _unlink_stale_lock(path):
            raise
    # A racer may create the fresh inode first; both then contend for its flock as usual.
    return _open_checked(path, _LOCK_FLAGS)


def _still_names(path: Path, fd: int) -> bool:
    """True when *path* is still the inode *fd* locked (a stale-lock replacement unlinks it)."""
    if os.name == 'nt':
        return True
    try:
        return _same_inode(os.lstat(path), os.fstat(fd))
    except FileNotFoundError:
        return False


#: Every ``ProfileOwnership`` alive in this process: the gateway's ``process_ownership``, an
#: ``exclusive_maintenance`` reservation (its own instance, possibly entered on another thread) and
#: any embedded owner. :func:`foreign_runtime_owner` asks them before probing a lock, because a flock
#: probe from this process would report its own reservation as held.
_live_ownerships: weakref.WeakSet[ProfileOwnership] = weakref.WeakSet()


class ProfileOwnership:
    def __init__(self, *, all_slots: bool = False):
        _live_ownerships.add(self)
        self._all_slots = all_slots
        self._handles: dict[Path, ExitStack] = {}
        self._slots: dict[Path, tuple[str, ...]] = {}
        self._writers: list[threading.Thread] = []
        self._mutex = threading.RLock()

    @property
    def homes(self) -> tuple[Path, ...]:
        with self._mutex:
            return tuple(self._handles)

    def reserve(self, homes, *, purpose: str | None = None) -> None:
        """``purpose`` marks a maintenance reservation in the lock record, so a contender can tell
        a short maintenance holder from a gateway runtime owner (:func:`live_runtime_owner`)."""
        from gateway.gateway_slot import SLOT_ACTIVE, SLOT_STANDBY, effective_gateway_slot, slot_filename
        from gateway.status import _try_acquire_file_lock, _release_file_lock, _build_pid_record
        # Runtime startup owns its own slot. Shared-state maintenance must exclude both.
        slots = (SLOT_ACTIVE, SLOT_STANDBY) if self._all_slots else (effective_gateway_slot(),)
        lock_names = tuple(slot_filename('gateway.lock', slot) for slot in slots)
        with self._mutex:
            added = []
            try:
                for home in sorted({canonical_home(p) for p in homes}):
                    if home in self._handles:
                        continue
                    make_user_dir(home, mode=0o700, parents=True, exist_ok=True)
                    with ExitStack() as locks:
                        for lock_name in lock_names:
                            path = home / lock_name
                            fd = _open_lock(path)
                            handle = locks.enter_context(os.fdopen(fd, 'r+', encoding='utf-8'))  # windows-footgun: ok — write-only
                            if not _try_acquire_file_lock(handle) or not _still_names(path, handle.fileno()):
                                raise OwnershipConflict(f'Gateway runtime already owns profile {home}')
                            locks.callback(_release_file_lock, handle)
                            record = {**_build_pid_record(), 'hermes_home': str(home)}
                            if purpose:
                                record['purpose'] = purpose
                            handle.seek(0)
                            handle.truncate()
                            json.dump(record, handle)
                            handle.flush()
                            os.fsync(handle.fileno())
                        self._handles[home] = locks.pop_all()
                        self._slots[home] = slots
                    added.append(home)
            except BaseException:
                for home in reversed(added):
                    self.release(home)
                raise

    def slots(self, home: Path) -> tuple[str, ...]:
        """The runtime slots whose ``gateway.lock`` this reservation holds for *home*."""
        with self._mutex:
            return self._slots.get(canonical_home(home), ())

    def owns(self, home: Path) -> bool:
        with self._mutex:
            return canonical_home(home) in self._handles

    def release(self, home: Path) -> None:
        with self._mutex:
            locks = self._handles.pop(canonical_home(home), None)
            self._slots.pop(canonical_home(home), None)
            if locks is not None:
                locks.close()

    def start_writer(self, thread: threading.Thread) -> None:
        """Register before starting so partial startup cannot forget a live writer."""
        with self._mutex:
            self._writers.append(thread)
            thread.start()

    def close(self) -> None:
        with self._mutex:
            # A timed-out daemon can still write during finally/atexit. Keep the
            # handles alive; process exit releases them atomically in the OS.
            if any(thread.is_alive() for thread in self._writers):
                return
            self._writers.clear()
            for home in reversed(self.homes):
                self.release(home)


process_ownership = ProfileOwnership()


def held_in_process(home: Path) -> bool:
    """True when any reservation in this process holds *home*'s runtime lock."""
    return bool(slots_held_in_process(home))


def slots_held_in_process(home: Path) -> frozenset[str]:
    """The slots of *home* whose runtime lock some reservation in this process holds."""
    home = canonical_home(home)
    return frozenset(slot for owner in list(_live_ownerships) for slot in owner.slots(home))


def _sibling_is_standby(home: Path, slot: str) -> bool:
    """The gateway holding *slot* answers its own status as an unpromoted zero-cut standby, which
    begins no epoch. Any failure to ask is not standby: unknown ownership never grants the store."""
    from gateway.control_socket import query_gateway_control
    status = query_gateway_control(home, "status", slot=slot, timeout=0.5)
    return bool(status) and status.get("standby") is True


def foreign_runtime_owner(home: Path, *, successor: bool = False) -> bool:
    """True when ANOTHER process holds a runtime lock of *home* that this caller may not begin under.

    Each slot whose lock this process does not hold is probed, including the sibling of a slot it
    does hold: a slot-b process that booted active beside a serving slot a must not take a's epoch.
    A sibling that answers as an unpromoted standby begins no epoch, so it does not block. The one
    other allowance is ``successor``, which only a zero-cut promotion passes: its predecessor still
    holds slot a while it finishes its in-flight turns. This is a foreign-owner refusal, not
    permission to write: assert_runtime_epoch_owner separately requires an exact-home reservation.
    """
    from gateway.gateway_slot import SLOT_ACTIVE, SLOT_STANDBY, slot_lock_held
    home = canonical_home(home)
    mine = slots_held_in_process(home)
    for slot in (SLOT_ACTIVE, SLOT_STANDBY):
        if slot in mine or not slot_lock_held(home, slot):
            continue
        if mine and (successor or _sibling_is_standby(home, slot)):
            continue
        return True
    return False


_maintenance = threading.local()


@contextmanager
def assert_runtime_epoch_owner(db_path):
    """Pin this process's exact-home ownership through an epoch write.

    Maintenance authority is synchronous and thread-local. Holding the owner's
    mutex prevents another thread from releasing the reservation mid-write.
    """
    home = Path(db_path).expanduser().resolve().parent
    owners = [process_ownership]
    state = getattr(_maintenance, 'state', None)
    if state is not None and state[0] == os.getpid():
        owners.append(state[1])
    for owner in owners:
        with owner._mutex:
            if owner.owns(home):
                yield
                return
    from hermes_state_runtime import RuntimeStoreError
    raise RuntimeStoreError('not_owner')


def maintenance_holder(home: Path, purpose: str) -> dict | None:
    """The live holder record of *home*'s ``gateway.lock`` when it is a ``purpose`` maintenance
    reservation (see :meth:`ProfileOwnership.reserve`), else None. Advisory only: it names the
    holder for a refusal or a short wait, it never grants the lock."""
    from gateway.status import _live_pid_from_record, _read_gateway_lock_record
    record = _read_gateway_lock_record(canonical_home(home) / 'gateway.lock')
    if not record or record.get('purpose') != purpose or _live_pid_from_record(record) is None:
        return None
    return record


def lock_record_unpublished(home: Path) -> bool:
    """True when *home*'s ``gateway.lock`` record names no live holder yet: empty, torn, or a dead
    previous holder's. :meth:`ProfileOwnership.reserve` takes the flock BEFORE it writes the record,
    so a contender that loses the flock in that window cannot tell maintenance from a gateway; it
    must retry (bounded) instead of reporting a runtime owner. Advisory only, never grants the lock."""
    from gateway.status import _live_pid_from_record, _read_gateway_lock_record
    record = _read_gateway_lock_record(canonical_home(home) / 'gateway.lock')
    return not record or _live_pid_from_record(record) is None


def live_runtime_owner(home: Path) -> int | None:
    """PID of a live runtime owner of *home* (a gateway, or a reservation that is not marked as
    maintenance), else None. A purpose-marked maintenance holder is never an owner."""
    from gateway.status import _live_pid_from_record, _read_gateway_lock_record, get_running_pid
    home = canonical_home(home)
    record = _read_gateway_lock_record(home / 'gateway.lock')
    live = _live_pid_from_record(record) if record else None
    if live is not None and record.get('purpose'):
        return None  # the flock holder is maintenance, so no gateway can hold it too
    return get_running_pid(home / 'gateway.pid', cleanup_stale=False) or live


@contextmanager
def exclusive_maintenance(homes, *, purpose: str | None = None):
    """Reserve every runtime slot before touching a home's managed state.

    This is not an owner-presence check: the reservation remains held through
    publication, excluding startup even before its PID/control socket exists.
    Only nested synchronous maintenance in this thread may reuse a reservation;
    being the runtime owner (even in this process) never grants maintenance.
    Lock inodes must not be restored, removed, or replaced by callers.
    """
    state = getattr(_maintenance, 'state', None)
    outermost = state is None or state[0] != os.getpid()
    owner = state[1] if state is not None and not outermost else ProfileOwnership(all_slots=True)
    previous = set(owner.homes)
    try:
        owner.reserve(homes, purpose=purpose)
        if outermost:
            _maintenance.state = (os.getpid(), owner)
        yield
    except OwnershipConflict as exc:
        raise OwnershipConflict(
            f'Exclusive maintenance refused: {exc}. Drain and stop the gateway, then retry.'
        ) from exc
    finally:
        for home in reversed(owner.homes):
            if home not in previous:
                owner.release(home)
        if outermost:
            _maintenance.state = None
