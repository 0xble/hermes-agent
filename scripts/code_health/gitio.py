"""Git plumbing: which files changed between two trees, and their contents on either side.

A tree is a revision (``"abc123"``) or ``None`` for the working tree. Both sides are read
through the same functions so base and head are measured identically.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel").strip())


def resolve_rev(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def resolve_tree(repo: Path, rev: str) -> str:
    """A head may be any tree-ish: a commit, or the index's tree from ``git write-tree``."""
    return git(repo, "rev-parse", "--verify", f"{rev}^{{tree}}").strip()


def commit_or_none(repo: Path, rev: str) -> str | None:
    """``rev`` as a commit sha, or None when it does not name a commit (a tree, a missing ref)."""
    out = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False)
    return out.strip() or None


def default_base(repo: Path, tip: str = "HEAD") -> str:
    """Merge-base of ``tip`` with origin/main (local runs); CI passes ``--base`` explicitly."""
    for ref in ("origin/main", "main"):
        out = git(repo, "merge-base", tip, ref, check=False).strip()
        if out:
            return out
    raise RuntimeError("no merge-base with origin/main or main; pass --base")


@dataclass(frozen=True)
class Change:
    status: str  # A, M, D, R
    old: str | None
    new: str | None


def _parse_name_status(out: str) -> list[Change]:
    fields = out.split("\0")
    changes: list[Change] = []
    i = 0
    while i < len(fields) and fields[i]:
        code = fields[i][0]
        if code in "RC":
            old, new = fields[i + 1], fields[i + 2]
            changes.append(Change("R" if code == "R" else "A", old if code == "R" else None, new))
            i += 3
            continue
        path = fields[i + 1]
        if code == "D":
            changes.append(Change("D", path, None))
        elif code == "A":
            changes.append(Change("A", None, path))
        else:
            changes.append(Change("M", path, path))
        i += 2
    return changes


def changed_files(repo: Path, base: str, head: str | None) -> list[Change]:
    args = ["diff", "--name-status", "-M", "-z", "--no-color", base]
    if head is not None:
        args.append(head)
    changes = _parse_name_status(git(repo, *args))
    if head is None:
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        changes.extend(Change("A", None, p) for p in untracked.split("\0") if p)
    return changes


def _is_partial_clone(repo: Path) -> bool:
    """True when this is a blob:none partial clone (prefetch before batch reads)."""
    out = git(repo, "config", "--get", "remote.origin.partialclonefilter", check=False).strip()
    return bool(out)


def _ensure_blobs(repo: Path, tree: str, paths: list[str]) -> None:
    """For partial clones only: list blob OIDs for the paths via ls-tree, identify
    missing ones via cat-file --batch-check, then one `git fetch --stdin` (or backfill)
    to avoid N per-file lazy fetches that each create a promisor pack.
    """
    if not paths or not _is_partial_clone(repo):
        return
    # ls-tree -r <tree> -- <paths> gives oids even if blobs are missing (tree entries exist)
    ls_out = git(repo, "ls-tree", "-r", "-z", tree, "--", *paths, check=False)
    oids: list[str] = []
    for part in ls_out.split("\0"):
        if part:
            fields = part.split(" ", 3)
            if len(fields) >= 3 and fields[1] == "blob" and len(fields[2]) == 40:
                oids.append(fields[2])
    if not oids:
        return
    # batch-check which oids are missing
    check_proc = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=repo,
        input="\n".join(oids) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    missing_oids = [line.split()[0] for line in check_proc.stdout.splitlines() if line.endswith(" missing")]
    if not missing_oids:
        return
    # one batched fetch of the missing blob OIDs (same pattern as scripts/ci/prefetch_tree.py)
    input_oids = "\n".join(missing_oids) + "\n"
    subprocess.run(
        [
            "git",
            "-c",
            "fetch.negotiationAlgorithm=noop",
            "fetch",
            "origin",
            "--no-tags",
            "--no-write-fetch-head",
            "--filter=blob:none",
            "--stdin",
        ],
        cwd=repo,
        input=input_oids,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def read_files(repo: Path, tree: str | None, paths: list[str]) -> dict[str, str | None]:
    """Batch version: one cat-file --batch (and optional one prefetch) for all paths.
    Returns dict path -> content or None (exactly as single read_file would).
    Handles binary via size, missing lines, utf-8-sig decode with errors=replace.
    """
    if tree is None:
        result: dict[str, str | None] = {}
        for path in paths:
            try:
                result[path] = (repo / path).read_text(encoding="utf-8-sig", errors="replace")
            except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
                result[path] = None
        return result
    if not paths:
        return {}
    _ensure_blobs(repo, tree, paths)
    specs = [f"{tree}:{p}" for p in paths]
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        input=("\n".join(specs) + "\n").encode("utf-8"),
        capture_output=True,
        timeout=120,
        check=False,
    )
    stdout = proc.stdout  # bytes for binary safety
    result = {}
    idx = 0
    spec_idx = 0
    while idx < len(stdout) and spec_idx < len(paths):
        nl_pos = stdout.find(b"\n", idx)
        if nl_pos == -1:
            break
        header = stdout[idx:nl_pos].decode("utf-8", errors="replace")
        idx = nl_pos + 1
        if header.endswith(" missing"):
            result[paths[spec_idx]] = None
            spec_idx += 1
            continue
        parts = header.split()
        if len(parts) != 3:
            result[paths[spec_idx]] = None
            spec_idx += 1
            continue
        _oid, typ, size_str = parts
        try:
            size = int(size_str)
        except ValueError:
            size = 0
        content = stdout[idx : idx + size]
        idx += size
        if idx < len(stdout) and stdout[idx : idx + 1] == b"\n":
            idx += 1
        path = paths[spec_idx]
        if typ == "blob":
            result[path] = content.decode("utf-8-sig", errors="replace")
        else:
            result[path] = None
        spec_idx += 1
    for p in paths:
        if p not in result:
            result[p] = None
    return result


def read_file(repo: Path, tree: str | None, path: str) -> str | None:
    """Unchanged public signature. Delegates to batched read_files for the single path."""
    return read_files(repo, tree, [path]).get(path)


def tracked_files(repo: Path, tree: str | None) -> list[str]:
    if tree is None:
        return [p for p in git(repo, "ls-files", "-z").split("\0") if p]
    return [p for p in git(repo, "ls-tree", "-r", "--name-only", "-z", tree).split("\0") if p]


def known_env_names(repo: Path, tree: str | None) -> set[str]:
    """Every HERMES_* literal in non-test Python of a tree (the HX002 allow set)."""
    args = ["grep", "-h", "-o", "-I", "-E", r"HERMES_[A-Z0-9_]+"]
    if tree is not None:
        args.append(tree)
    args += ["--", "*.py", ":!tests/**", ":!**/tests/**"]
    out = git(repo, *args, check=False)
    names = set()
    for line in out.splitlines():
        # `git grep <tree>` prefixes "<tree>:" even with -h on some versions.
        names.add(line.rsplit(":", 1)[-1])
    return names
