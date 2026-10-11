"""Conservative Linux ownership evidence for stale Git-resource recovery."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional

_PROC_ROOT = Path("/proc")
_linux_process_scope = sys.platform == "linux"


def _repository_dirs(cwd: Path, git_dir: Optional[str] = None) -> set[Path]:
    """Resolve ordinary, bare and linked-worktree metadata without executing Git."""
    if git_dir is not None:
        metadata = (cwd / git_dir).resolve()
    else:
        metadata = None
        for parent in (cwd, *cwd.parents):
            marker = parent / ".git"
            if marker.is_dir():
                metadata = marker.resolve()
                break
            if marker.is_file():
                prefix, separator, relative = marker.read_text().strip().partition(":")
                if prefix != "gitdir" or not separator or not relative.strip():
                    raise ValueError("unrecognized gitfile")
                metadata = (parent / relative.strip()).resolve()
                break
            if (parent / "HEAD").is_file() and (parent / "objects").is_dir():
                metadata = parent.resolve()
                break
        if metadata is None:
            raise ValueError("repository ownership unavailable")
    directories = {metadata}
    common = metadata / "commondir"
    if common.is_file():
        directories.add((metadata / common.read_text().strip()).resolve())
    stores = {(directory / "objects").resolve() for directory in directories}
    directories.update(stores)
    directories.update((store / "pack").resolve() for store in stores)
    return directories


def _process_repository_dirs(proc: Path) -> set[Path]:
    cwd = proc.joinpath("cwd").resolve(strict=True)
    env = dict(item.split("=", 1) for item in os.fsdecode(proc.joinpath("environ").read_bytes()).split("\0")
               if "=" in item)
    argv = os.fsdecode(proc.joinpath("cmdline").read_bytes()).strip("\0").split("\0")
    # Alternate object stores and inline config can redirect ownership beyond cwd.
    # Do not guess: unknown ownership retains the global safety veto.
    redirected = ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                  "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
    if any(env.get(key) for key in redirected):
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
        elif arg == "-c" or arg.startswith(("-c", "--config-env", "--work-tree", "--bare")):
            raise ValueError("explicit Git configuration")
        elif not arg.startswith("-"):
            # upload-pack/receive-pack take a repository argument rather than cwd.
            command = arg if Path(argv[0]).name == "git" else Path(argv[0]).name.removeprefix("git-")
            if command in ("upload-pack", "receive-pack"):
                git_dir = next(args) if Path(argv[0]).name == "git" else arg
            elif command in ("clone", "init"):
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
    except (OSError, ValueError):
        return True
    for proc in processes:
        if not proc.name.isdecimal():
            continue
        try:
            name = proc.joinpath("comm").read_text().strip()
            if name != "git" and not name.startswith("git-"):
                continue
            if target & _process_repository_dirs(proc):
                return True
        except (OSError, ValueError, StopIteration):
            # A vanished process is dead; inaccessible ownership of a live one
            # is NOT evidence that it is safe to delete its resources.
            if proc.exists():
                return True
    return False


