"""Immutable revision preparation for ``hermes update --revision``.

The pinned path deliberately uses only exact object IDs.  It never discovers a
branch tip, merges upstream, stashes a dirty checkout, or falls back to ZIP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
import json
import os
import tempfile
from typing import Any, Callable

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFESTS = (
    "pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt",
    "package.json", "package-lock.json", "npm-shrinkwrap.json",
)


@dataclass(frozen=True)
class RevisionTarget:
    """A fetched, verified commit selected by the caller."""

    sha: str
    tree: str


@dataclass(frozen=True)
class RevisionRollback:
    """Pre-checkout source rollback anchor and dependency manifest inventory.

    This restores source only.  Dependency environments are intentionally not
    snapshotted or restored by this feature.
    """

    ref: str
    source_sha: str
    manifests: dict[str, str]
    receipt_path: str = ""


def validate_revision(value: object) -> str:
    """Accept only the literal lower-case 40-character SHA supplied by the user."""
    if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
        raise ValueError("--revision must be an exact 40-character lowercase commit SHA")
    return value


def _run(git_run: Callable[..., Any], git_cmd: list[str], args: list[str], cwd: Path, *, network: bool = False) -> Any:
    return git_run(git_cmd, args, cwd, network=network)


def _stdout(result: Any) -> str:
    return str(getattr(result, "stdout", "")).strip()


def _require_ok(result: Any, message: str) -> str:
    if getattr(result, "returncode", 1) != 0:
        detail = str(getattr(result, "stderr", "")).strip().splitlines()
        raise RuntimeError(f"{message}{': ' + detail[0] if detail else ''}")
    return _stdout(result)


def _ensure_clean_and_not_divergent(git_run: Callable[..., Any], git_cmd: list[str], cwd: Path) -> None:
    dirty = _require_ok(_run(git_run, git_cmd, ["status", "--porcelain"], cwd), "Could not inspect working tree")
    if dirty:
        raise RuntimeError("Pinned revision update refuses a dirty working tree; commit or discard changes first")
    upstream = _run(git_run, git_cmd, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
    if getattr(upstream, "returncode", 1) != 0:
        return  # Detached or non-tracking checkouts have no branch divergence to reconcile.
    counts = _require_ok(
        _run(git_run, git_cmd, ["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd),
        "Could not inspect branch divergence",
    ).split()
    if len(counts) == 2 and int(counts[0]) and int(counts[1]):
        raise RuntimeError("Pinned revision update refuses a divergent branch; reconcile it manually first")


def prepare_revision_target(git_run: Callable[..., Any], git_cmd: list[str], cwd: Path, revision: object) -> RevisionTarget:
    """Reject unsafe state, fetch the requested object, and prove its commit/tree.

    No working-tree or service mutation occurs in this preparation boundary.
    """
    sha = validate_revision(revision)
    _ensure_clean_and_not_divergent(git_run, git_cmd, cwd)
    _require_ok(_run(git_run, git_cmd, ["fetch", "origin", sha], cwd, network=True), f"Could not fetch requested revision {sha}")
    resolved = _require_ok(
        _run(git_run, git_cmd, ["rev-parse", "--verify", f"{sha}^{{commit}}"], cwd),
        f"Requested revision {sha} is unavailable or is not a commit",
    )
    if resolved != sha:
        raise RuntimeError(f"Requested revision resolved unexpectedly ({resolved}); refusing mutable reference")
    tree = _require_ok(
        _run(git_run, git_cmd, ["rev-parse", "--verify", f"{sha}^{{tree}}"], cwd),
        f"Requested revision {sha} has no source tree",
    )
    for name in ('hermes_cli/main.py', 'gateway/run.py', 'run_agent.py'):
        source = _run(git_run, git_cmd, ['show', f'{sha}:{name}'], cwd)
        _require_ok(source, f'Requested revision is missing {name}')
        try:
            compile(source.stdout, f'{sha}:{name}', 'exec', dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            raise RuntimeError(f'Requested revision cannot compile {name} with this interpreter') from exc
    return RevisionTarget(sha=sha, tree=tree)


def retain_precheckout_rollback(git_run: Callable[..., Any], git_cmd: list[str], cwd: Path, target: RevisionTarget) -> RevisionRollback:
    """Retain and read back the source rollback ref before a pinned checkout."""
    source_sha = _require_ok(_run(git_run, git_cmd, ["rev-parse", "HEAD"], cwd), "Could not capture pre-update HEAD")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    ref = f"refs/hermes-update-backups/revision-{stamp}-{source_sha[:12]}-to-{target.sha[:12]}"
    _require_ok(_run(git_run, git_cmd, ["update-ref", ref, source_sha], cwd), "Could not retain source rollback ref")
    readback = _require_ok(_run(git_run, git_cmd, ["rev-parse", "--verify", ref], cwd), "Could not read back source rollback ref")
    if readback != source_sha:
        raise RuntimeError("Source rollback ref readback did not match pre-update HEAD")
    manifests: dict[str, str] = {}
    for name in _MANIFESTS:
        path = cwd / name
        if path.is_file():
            manifests[name] = sha256(path.read_bytes()).hexdigest()
    raw_dir = _require_ok(_run(git_run, git_cmd,
        ['rev-parse', '--git-path', 'hermes-update-revisions'], cwd),
        'Could not resolve revision evidence directory')
    directory = Path(raw_dir)
    if not directory.is_absolute():
        directory = cwd / directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{ref.rsplit("/", 1)[-1]}.json'
    evidence = {'schema': 1, 'phase': 'prepared', 'requested_sha': target.sha,
                'tree': target.tree, 'rollback_ref': ref, 'rollback_sha': source_sha,
                'dependency_manifests': manifests, 'rollback_scope': 'source-only'}
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, delete=False) as stream:
        json.dump(evidence, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    try:
        os.replace(temporary, path)
        if json.loads(path.read_text(encoding="utf-8")) != evidence:
            raise RuntimeError('Prepared revision evidence readback failed')
    finally:
        temporary.unlink(missing_ok=True)
    return RevisionRollback(ref=ref, source_sha=source_sha, manifests=manifests,
                            receipt_path=str(path))


def checkout_revision(git_run: Callable[..., Any], git_cmd: list[str], cwd: Path, target: RevisionTarget) -> None:
    """Detach exactly at the prepared commit and prove the checkout identity."""
    _ensure_clean_and_not_divergent(git_run, git_cmd, cwd)
    _require_ok(_run(git_run, git_cmd, ["checkout", "--detach", target.sha], cwd), f"Could not checkout requested revision {target.sha}")
    verify_revision_head(git_run, git_cmd, cwd, target.sha)


def verify_revision_head(git_run: Callable[..., Any], git_cmd: list[str], cwd: Path, revision: str) -> str:
    """Fail closed unless the checkout is exactly the approved immutable object."""
    actual = _require_ok(_run(git_run, git_cmd, ["rev-parse", "HEAD"], cwd), "Could not verify checkout HEAD")
    if actual != revision:
        raise RuntimeError(f"Checkout HEAD is {actual}, not approved revision {revision}")
    return actual


def record_revision_receipt(target: RevisionTarget, rollback: RevisionRollback | None = None) -> None:
    """Attach immutable target and source-only rollback evidence to the active receipt."""
    try:
        import hermes_cli.update_receipt as receipt
        if receipt._current is not None:
            data = receipt._current.data
            revision_data: dict[str, Any] = {"requested_sha": target.sha, "tree": target.tree}
            data["revision"] = revision_data
            if rollback is not None:
                rollback_data: dict[str, Any] = {
                    "source_ref": rollback.ref,
                    "prepared_receipt": rollback.receipt_path,
                    "source_sha": rollback.source_sha,
                    "dependency_manifests": rollback.manifests,
                    "scope": "source-only; dependency environment rollback is not implemented",
                }
                data["revision"]["rollback"] = rollback_data
    except Exception:
        pass
