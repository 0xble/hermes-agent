"""Conservative Linux ownership evidence for stale Git-resource recovery."""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Callable, Optional

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
        raise ValueError("launch cwd cannot be reconstructed")
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
    redirected = ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
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
            raise ValueError("launch cwd cannot be reconstructed")
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
            if arg in ("clone", "init"):
                raise ValueError("Git may create a repository outside cwd")
            break
    return _repository_dirs(cwd, git_dir)


def git_process_running_for(repo_root: Path, fallback: Callable[[], bool]) -> bool:
    """Veto target/unknown Git owners, not transfers in unrelated Linux checkouts.

    Other platforms retain the existing global probe. On Linux include dashed
    helpers; age alone never proves their lock or pack is abandoned.
    """
    if not _linux_process_scope:
        return fallback()
    try:
        target = _repository_dirs(Path(repo_root).resolve())
        processes = list(_PROC_ROOT.iterdir())
    except (OSError, RuntimeError, ValueError):
        return True
    for proc in processes:
        if not proc.name.isdecimal():
            continue
        try:
            name = proc.joinpath("comm").read_text(encoding="utf-8-sig").strip()
            if name != "git" and not name.startswith("git-"):
                continue
            if target & _process_repository_dirs(proc):
                return True
        except (OSError, RuntimeError, ValueError, StopIteration):
            # A vanished process is dead; inaccessible ownership of a live one
            # is NOT evidence that it is safe to delete its resources.
            if proc.exists():
                return True
    return False


