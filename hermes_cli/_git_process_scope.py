"""Conservative Linux ownership evidence for stale Git-resource recovery."""
from __future__ import annotations

import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Callable, Optional

class _UnplacedGit(ValueError):
    """Launch-relative ownership needs file-holder proof for packs, never locks."""


_PROC_ROOT = Path("/proc")
_linux_process_scope = sys.platform == "linux"

# The updater's custody flags (update_custody.GIT_NO_DETACH / _MUTATOR_CONFIG) on every Git it
# runs. None of them moves the repository, object store, index or pack output, so they leave
# ownership provable from cwd. Any other inline config may (core.worktree, include.path...).
_OWNERSHIP_NEUTRAL_CONFIG = frozenset({
    "gc.autodetach=false", "maintenance.auto=false", "core.fsmonitor=false", "credential.helper=",
    f"core.hookspath={os.devnull}",
})


def _neutral_config(item: str) -> bool:
    key, separator, value = item.partition("=")
    return bool(separator) and f"{key.lower()}={value}" in _OWNERSHIP_NEUTRAL_CONFIG


def _inherited_config_is_neutral(parameters: str) -> bool:
    # Git exports parent -c pairs to children as shell-quoted 'key=value' or 'key'='value'.
    try:
        items = shlex.split(parameters)
    except ValueError:
        return False
    return all(_neutral_config(item) for item in items)


def _repository_dirs(cwd: Path, git_dir: Optional[str] = None) -> set[Path]:
    """Resolve ordinary, bare and linked-worktree metadata without executing Git."""
    if git_dir is not None:
        metadata = (cwd / git_dir).resolve()
        # An explicit --git-dir/GIT_DIR that does not name a repository Git would accept is
        # unknown ownership, not proof the process is elsewhere. Git may have chdir'd (for
        # example into core.worktree) after reading it, so a relative path no longer resolves.
        if not metadata.is_dir() or not _is_git_directory(metadata):
            raise ValueError("explicit git dir is not a repository here")
    else:
        metadata = None
        for parent in (cwd, *cwd.parents):
            marker = parent / ".git"
            if marker.is_dir():
                # Git skips metadata it rejects and keeps walking: never stop at it, never guess past it.
                if not _is_git_directory(marker):
                    raise ValueError("repository metadata Git would reject")
                metadata = marker.resolve()
                break
            if marker.is_file():
                prefix, separator, relative = marker.read_text(encoding="utf-8-sig").strip().partition(":")
                if prefix != "gitdir" or not separator or not relative.strip():
                    raise ValueError("unrecognized gitfile")
                metadata = (parent / relative.strip()).resolve()
                break
            if (parent / "HEAD").is_file() and (parent / "objects").is_dir():
                if not _is_git_directory(parent):
                    raise ValueError("repository metadata Git would reject")
                metadata = parent.resolve()
                break
        if metadata is None:
            raise ValueError("repository ownership unavailable")
    directories = {metadata}
    common = metadata / "commondir"
    if common.is_file():
        directories.add((metadata / common.read_text(encoding="utf-8-sig").strip()).resolve())
    stores = {(directory / "objects").resolve() for directory in directories}
    directories.update(stores)
    directories.update((store / "pack").resolve() for store in stores)
    return directories


# upload-pack/receive-pack parse their own options (parse-options, so they may follow the
# operand) and take exactly one repository operand. Anything else is not provable.
_SERVER_FLAGS = frozenset({
    "-q", "--quiet", "--stateless-rpc", "--advertise-refs", "--http-backend-info-refs", "--strict",
    "--no-strict", "--skip-connectivity-check", "--reject-thin-pack-for-testing",
})


_HEX_OID = re.compile(rb"[0-9a-fA-F]{40}")


def _valid_head(head: Path) -> bool:
    """Git's validate_headref() for a regular HEAD file."""
    if head.is_symlink():
        # Git's symlink rule has differed across versions; an unproven choice keeps the veto.
        raise ValueError("symlinked HEAD")
    if not head.is_file():
        return False
    with head.open("rb") as stream:
        content = stream.read(255).partition(b"\0")[0]
    if content.startswith(b"ref:"):
        if content[4:].lstrip(b" \t\n\r").startswith(b"refs/"):
            return True
    return _HEX_OID.match(content) is not None


def _is_git_directory(path: Path) -> bool:
    # Git's is_git_directory(): a valid HEAD plus objects/ and refs/, in the common dir when
    # linked. enter_repo() skips a candidate whose HEAD Git rejects, so this must reject it too.
    common = path / "commondir"
    store = path / common.read_text(encoding="utf-8-sig").strip() if common.is_file() else path
    return (store / "objects").is_dir() and (store / "refs").is_dir() and _valid_head(path / "HEAD")


def _server_repository(operands: list[str]) -> str:
    """The metadata directory a server command enters, as Git's enter_repo() finds it."""
    paths, strict, options = [], False, True
    args = iter(operands)
    for arg in args:
        if options and arg == "--":
            options = False
        elif options and arg == "--timeout":
            next(args)
        elif options and arg.startswith("--timeout="):
            continue
        elif options and arg in _SERVER_FLAGS:
            strict = arg == "--strict" or strict and arg != "--no-strict"
        elif options and arg.startswith("-"):
            raise ValueError("unrecognized server option")
        else:
            paths.append(arg)
    if len(paths) != 1:
        raise ValueError("server repository operand missing or ambiguous")
    path = paths[0]
    if not os.path.isabs(path):
        # enter_repo() resolves it against the launch cwd and then chdirs, so /proc's cwd
        # may already be the repository (or not yet): its owner cannot be reconstructed.
        raise _UnplacedGit("launch cwd cannot be reconstructed")
    base = path.rstrip("/") or "/"
    for suffix in ("",) if strict else ("/.git", "", ".git/.git", ".git"):
        candidate = Path(base + suffix)
        if candidate.is_file():
            prefix, separator, relative = candidate.read_text(encoding="utf-8-sig").strip().partition(":")
            if prefix != "gitdir" or not separator or not relative.strip():
                raise ValueError("unrecognized gitfile")
            # Git commits to a gitfile and fails if its target is invalid; it never falls through.
            target = candidate.parent / relative.strip()
            if not _is_git_directory(target):
                raise ValueError("gitfile target is not a repository")
            return str(target)
        if candidate.is_dir() and _is_git_directory(candidate):
            return str(candidate)
    raise ValueError("server repository unavailable")


def _process_repository_dirs(proc: Path) -> set[Path]:
    cwd = proc.joinpath("cwd").resolve(strict=True)
    env = dict(item.split("=", 1) for item in os.fsdecode(proc.joinpath("environ").read_bytes()).split("\0")
               if "=" in item)
    argv = os.fsdecode(proc.joinpath("cmdline").read_bytes()).strip("\0").split("\0")
    # Alternate object stores and inline config can redirect ownership beyond cwd.
    # Do not guess: unknown ownership retains the global safety veto.
    # GIT_INDEX_FILE is deliberately absent: Git exports it for ordinary commits
    # (".git/index") and the index location never chooses the object store a
    # process writes packs into. Repository ownership still comes from cwd/GIT_DIR.
    redirected = ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
                  "GIT_CONFIG_COUNT")
    if any(env.get(key) for key in redirected):
        raise ValueError("explicit resource/config ownership")
    if env.get("GIT_CONFIG_PARAMETERS") and not _inherited_config_is_neutral(env["GIT_CONFIG_PARAMETERS"]):
        raise ValueError("explicit resource/config ownership")
    git_dir = env.get("GIT_DIR")
    # Plumbing can name pack outputs outside its cwd, including our tmp_*
    # namespace. Only their normal repository-local stream modes are provable.
    for command in ("index-pack", "pack-objects"):
        if command in (Path(argv[0]).name.removeprefix("git-"), *argv[1:]):
            operands = argv[argv.index(command) + 1:] if command in argv else argv[1:]
            if (any(arg == "--" or not arg.startswith("-") or arg.startswith("-o") for arg in operands)
                    or command == "pack-objects" and "--stdout" not in operands
                    or command == "index-pack" and "--stdin" not in operands):
                raise ValueError("explicit pack output ownership")
    program = Path(argv[0]).name.removeprefix("git-")
    if program in ("upload-pack", "receive-pack"):
        # Dashed server helpers have no git-level options: every argument is the server's.
        return _repository_dirs(cwd, _server_repository(argv[1:]))
    args = iter(argv[1:])
    for arg in args:
        if arg == "-C" or arg.startswith("-C"):
            # /proc cwd may already reflect -C; replaying a relative launch path
            # can misidentify an outer repository. Retain the veto.
            raise _UnplacedGit("launch cwd cannot be reconstructed")
        elif arg == "--git-dir":
            git_dir = next(args)
        elif arg.startswith("--git-dir="):
            git_dir = arg.partition("=")[2]
        elif arg == "-c" and _neutral_config(next(args, "")):
            continue
        elif arg.startswith("-c") and arg != "-c" and _neutral_config(arg[2:]):
            continue
        elif arg == "-c" or arg.startswith(("-c", "--config-env", "--work-tree", "--bare")):
            raise ValueError("explicit Git configuration")
        elif not arg.startswith("-"):
            if arg in ("upload-pack", "receive-pack"):
                # The server enters its operand repository (enter_repo), not cwd or GIT_DIR.
                return _repository_dirs(cwd, _server_repository(list(args)))
            if arg == "clone":
                raise _UnplacedGit("Git may create a repository outside cwd")
            if arg == "init":
                raise ValueError("Git may create a repository outside cwd")
            break
    return _repository_dirs(cwd, git_dir)


def _unplaced_pack_owner(proc: Path, target: set[Path], repo_root: Path) -> bool:
    # Do not replay -C or a server operand against post-enter_repo cwd. Retain a
    # visible local/shared owner and explicit paths; the holder scan covers the
    # unplaced remainder. Rejected metadata is still unknown, not unrelated.
    from hermes_cli._early_recovery import git_works_in

    cwd = proc.joinpath("cwd").resolve(strict=True)
    try:
        if target & _repository_dirs(cwd):
            return True
    except ValueError as exc:
        if str(exc) != "repository ownership unavailable":
            raise
    argv = os.fsdecode(proc.joinpath("cmdline").read_bytes()).strip("\0").split("\0")
    # A -C/clone exception may precede still-unparsed global or clone config.
    # Permit custody pairs only; do not silently lose those original vetoes.
    args = iter(argv[1:])
    for arg in args:
        if arg == "-c":
            if not _neutral_config(next(args, "")):
                return True
        elif arg.startswith("-c") and not _neutral_config(arg[2:]):
            return True
        elif arg.startswith(("--config", "--work-tree", "--bare", "--git-dir", "--separate-git-dir")):
            return True
    env = proc.joinpath("environ").read_bytes().split(b"\0")
    for entry in env:
        if entry.startswith(b"GIT_DIR=") and entry != b"GIT_DIR=":
            git_dir = os.fsdecode(entry.partition(b"=")[2])
            # A relative GIT_DIR depends on the lost launch cwd and stays a veto.
            # Git exports an absolute one for linked worktrees (-C <worktree>);
            # that names its repository exactly, so compare it with the target.
            if not os.path.isabs(git_dir):
                return True
            if target & _repository_dirs(cwd, git_dir):
                return True
    places = tuple(str(p) for p in target | {Path(repo_root).resolve()})
    if git_works_in(argv, str(cwd), None, places):
        return True
    # git_works_in compares lexically. A target reached through a symlinked
    # -C or operand path is still the target, so resolve path-like arguments
    # (realpath only reads link metadata) before ruling the process unrelated.
    # Git applies each relative -C to the previous -C directory, so resolve
    # against every base in that chain. Over-matching only adds vetoes.
    values = [arg.partition("=")[2] if arg.startswith("-") else arg for arg in argv[1:]]
    bases = [str(cwd)]
    args = iter(argv[1:])
    for arg in args:
        step = next(args, "") if arg == "-C" else arg[2:] if arg.startswith("-C") else None
        if step:
            bases.append(os.path.realpath(os.path.join(bases[-1], step)))
    resolved = {os.path.realpath(os.path.join(base, value)) for base in bases for value in values if value}
    return git_works_in(["git", *bases, *resolved], None, None, places)


def _pack_held_proc(proc: Path, path: Path) -> bool:
    """An fd, directory handle or mmap retains a pack; incomplete visibility vetoes.

    Compare inode/device, not link text: hardlinks and mmap with a closed fd are
    live too. Unlike _early_recovery._held_open_proc's fd-only mode, errors must
    not become proof of absence. Git keeps transfer temps open until promotion;
    known repository owners are additionally retained even with closed handles.
    """
    identity = {(st.st_dev, st.st_ino) for st in (path.stat(), path.parent.stat())}
    for fd in proc.joinpath("fd").iterdir():
        try:
            st = fd.stat()
        except FileNotFoundError:
            try:
                os.readlink(fd)
            except FileNotFoundError:
                continue  # descriptor disappeared, not an unreadable live handle
            raise  # broken target is not negative holder proof
        if (st.st_dev, st.st_ino) in identity:
            return True
    for line in proc.joinpath("maps").read_text(encoding="utf-8-sig", errors="replace").splitlines():
        fields = line.split(maxsplit=5)
        major, minor = (int(part, 16) for part in fields[3].split(":"))
        if (os.makedev(major, minor), int(fields[4])) in identity:
            return True
    return False


def _exited(proc: Path) -> bool:
    """The process is gone, or is a single-threaded zombie that holds nothing.

    A zombie has released its descriptors, mappings and cwd and can never run again, so
    it can neither hold nor write a pack. Its unreadable cwd/environ/fd is not unknown
    ownership. A zombie thread-group leader with live threads (``Threads:`` > 1) is still
    a running process and stays a veto, as does any status we cannot read.
    """
    try:
        status = proc.joinpath("status").read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        return not proc.exists()  # only a vanished /proc/<pid> proves the process is gone
    except OSError:
        return False
    fields: dict[str, str] = {}
    for line in status.splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    state = fields.get("State", "")[:1]
    threads = fields.get("Threads", "")
    return state in ("Z", "X") and threads.isdecimal() and int(threads) <= 1


# Short-lived processes on a busy host (an exiting Git whose cwd is already gone, a child
# mid-exec whose environ is briefly unreadable) fail inspection for milliseconds. Such a
# veto is re-checked within this per-sweep budget; positive ownership or holder proof,
# and any inspection failure that persists for the whole budget, still veto.
INSPECTION_RETRY_SECONDS = 2.0
_INSPECTION_RETRY_PAUSE = 0.05


def tmp_pack_in_use(repo_root: Path, path: Path, fallback: Callable[[], bool], *,
                    retry_until: Optional[float] = None) -> bool:
    """Packs need holder proof; launch-relative Git elsewhere need not veto them.

    Unknown metadata/config/output shapes and inaccessible live processes remain vetoes.
    A veto that rests only on a failed inspection is re-scanned until ``retry_until``
    (a ``time.monotonic()`` deadline), so a transient process state on a busy host does
    not strand the candidate; the answer is still a complete scan with no veto.
    Non-Linux retains the existing repository/global probe; stale locks remain on
    the separate global process guard in gitlock.py.
    """
    if not _linux_process_scope:
        return fallback()
    while True:
        verdict = _scan_pack_owners(repo_root, path)
        if verdict is not _INSPECTION_FAILED:
            return verdict
        if retry_until is None or time.monotonic() >= retry_until:
            return True
        time.sleep(_INSPECTION_RETRY_PAUSE)


_INSPECTION_FAILED = object()


def _scan_pack_owners(repo_root: Path, path: Path):
    """True on ownership/holder proof, False when no process vetoes, else _INSPECTION_FAILED."""
    try:
        target = _repository_dirs(Path(repo_root).resolve())
    except (OSError, RuntimeError, ValueError):
        # The sweep itself already proved this is a supported local pack layout
        # (.git/objects/pack or objects/pack). Keep that layout's scope usable
        # for callers with a deliberately minimal repository fixture, while
        # retaining conservative holder checks below for inaccessible processes.
        pack_dir = path.parent.resolve()
        target = {Path(repo_root).resolve(), pack_dir, pack_dir.parent}
    try:
        processes = list(_PROC_ROOT.iterdir())
    except (OSError, RuntimeError, ValueError):
        return True
    failed = False
    for proc in processes:
        if not proc.name.isdecimal():
            continue
        name = None
        try:
            name = proc.joinpath("comm").read_text(encoding="utf-8-sig").strip()
            if name == "git" or name.startswith("git-"):
                try:
                    if target & _process_repository_dirs(proc):
                        return True
                except _UnplacedGit:
                    if _unplaced_pack_owner(proc, target, repo_root):
                        return True
            if _pack_held_proc(proc, path):
                return True
        except (OSError, RuntimeError, ValueError, StopIteration, IndexError):
            if _exited(proc):
                continue  # gone or a zombie mid-reap: it holds and writes nothing
            try:
                uid = proc.stat().st_uid
            except FileNotFoundError:
                continue
            except OSError:
                failed = True
                continue
            if _uninspectable_non_git(name, uid, path.parent):
                continue
            failed = True
    return _INSPECTION_FAILED if failed else False


def _uninspectable_non_git(name: Optional[str], uid: int, directory: Path) -> bool:
    """Another user's non-Git process whose fd/maps we may not read cannot veto a sweep.

    A non-root sweeper can never read root's or other users' fd/maps, so treating that as a
    veto would stop every sweep on an ordinary host. Only Git creates, promotes or renames
    ``tmp_*`` packs; a non-Git reader keeps its inode after unlink, so its data is safe.
    Git (by name), our own uid, and an unknown name for a process that could write the
    directory (as in _early_recovery._could_write) remain vetoes.
    """
    if uid == os.geteuid():  # windows-footgun: ok — Linux-only path (_linux_process_scope)
        return False
    if name is not None:
        return not (name == "git" or name.startswith("git-"))
    try:
        st = os.stat(directory)
    except OSError:
        return False
    return not (uid in (0, st.st_uid) or st.st_mode & 0o022)
