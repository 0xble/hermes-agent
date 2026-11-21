"""Filesystem-boundary walkers shared by Hermes backup surfaces."""

import logging
import os
import stat
from pathlib import Path
from typing import Optional


logger = logging.getLogger("hermes_cli.backup")


def _is_non_regular_path(path: Path) -> bool:
    try:
        return not stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _is_link_path(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return os.name == "nt" and bool(getattr(info, "st_file_attributes", 0)
                                      & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def device_number(path: Path) -> Optional[int]:
    """Return a directory entry's device number without following symlinks."""
    try:
        return os.lstat(path).st_dev
    except OSError:
        # Keep unreadable entries in the walk so the archive writer can report the
        # read failure instead of silently claiming a complete backup.
        return None


def is_mount_or_foreign_filesystem(path: Path, root_device: int) -> bool:
    """True when *path* is a mount point or lives on another filesystem."""
    device = device_number(path)
    return os.path.ismount(path) or (device is not None and device != root_device)


def record_skipped_mount(path: Path, root: Path, skipped_dirs: Optional[set]) -> None:
    """Record and log one skipped mount/foreign-filesystem directory."""
    rel = path.relative_to(root).as_posix()
    if skipped_dirs is not None:
        skipped_dirs.add(rel)
    logger.info("backup skipped mount point=%s", rel)


def iter_external_files(
    base: Path,
    excluded_dirs: set[str],
    excluded_names: set[str],
    excluded_suffixes: tuple[str, ...],
    walk_root: Optional[Path] = None,
) -> list[Path]:
    """Return regular files under *base* without crossing filesystem boundaries."""
    if _is_link_path(base):
        return []
    if base.is_file():
        return [base]
    if not base.is_dir():
        return []
    root = walk_root or base
    root_device = device_number(root)
    if root_device is None:
        return []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        kept = []
        for name in dirnames:
            candidate = Path(dirpath) / name
            if _is_link_path(candidate):
                continue
            if is_mount_or_foreign_filesystem(candidate, root_device):
                record_skipped_mount(candidate, root, None)
                continue
            if name not in excluded_dirs:
                kept.append(name)
        dirnames[:] = kept
        files.extend(
            fp for fp in (Path(dirpath) / f for f in filenames)
            if not (_is_non_regular_path(fp) or fp.name in excluded_names
                    or fp.name.endswith(excluded_suffixes))
        )
    return files


def iter_quick_snapshot_candidates(
    home: Path, quick_state_files: tuple[str, ...], skipped_mounts: Optional[set[str]] = None,
):
    """Yield quick-snapshot files without crossing links or filesystem boundaries."""
    root_device = device_number(home)
    if root_device is None:
        return
    for rel in quick_state_files:
        src = home / rel
        if is_mount_or_foreign_filesystem(src, root_device):
            if skipped_mounts is not None:
                skipped_mounts.add(rel)
            logger.info("quick snapshot skipped mount point=%s", rel)
            continue
        if src.is_dir() and not _is_link_path(src):
            for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
                rel_dir = Path(dirpath).relative_to(home)
                kept = []
                for name in dirnames:
                    candidate = Path(dirpath) / name
                    candidate_rel = (rel_dir / name).as_posix()
                    if is_mount_or_foreign_filesystem(candidate, root_device):
                        if skipped_mounts is not None:
                            skipped_mounts.add(candidate_rel)
                        logger.info("quick snapshot skipped mount point=%s", candidate_rel)
                        continue
                    if name in {"workspaces", "attachments"} or _is_link_path(candidate):
                        continue
                    kept.append(name)
                dirnames[:] = kept
                for name in filenames:
                    candidate = Path(dirpath) / name
                    if _is_non_regular_path(candidate):
                        continue
                    yield candidate, candidate.relative_to(home).as_posix(), True
        elif src.is_file() and not _is_link_path(src):
            yield src, rel, False
