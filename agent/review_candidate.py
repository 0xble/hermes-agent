"""Immutable candidate evidence and typed native-review results.

This module captures Git evidence with read-only commands.  It does not stage,
reset, checkout, or otherwise write the caller's index or worktree.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
import stat
import subprocess
from typing import Any, Iterable, Mapping


_CANDIDATE_CONTRACT = "review_candidate_v1"
_JUDGMENT_CONTRACT = "native_review_judgment_v1"
_RESULT_CONTRACT = "native_review_result_v1"
_JUDGMENTS = frozenset({"approve", "request_changes", "needs_human"})

# Fail-closed evidence budget. Every captured byte is held in memory, base64
# encoded, serialized into the candidate identity, and sent in the reviewer
# prompt by the parent CLI/gateway process, so capture rejects oversized
# evidence outright rather than truncating it or exhausting that process.
MAX_UNTRACKED_FILE_BYTES = 2 * 1024 * 1024
"""Largest single untracked file (or symlink target) capture will read."""
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
"""Largest tracked patch plus all untracked content one candidate may carry."""


class ReviewCandidateStale(ValueError):
    """The repository no longer matches the candidate sent for review."""


@dataclass(frozen=True)
class ReviewUntrackedFileV1:
    path: str
    mode: str
    content_base64: str
    sha256: str


@dataclass(frozen=True)
class ReviewCandidateV1:
    repository: str
    base_commit: str
    head_commit: str
    accepted_scope: tuple[str, ...]
    tracked_patch: str
    untracked_files: tuple[ReviewUntrackedFileV1, ...]
    candidate_id: str
    tracked_patch_sha256: str = ""
    contract: str = field(default=_CANDIDATE_CONTRACT, init=False)

    def evidence_payload(self) -> dict[str, Any]:
        """Complete, JSON-safe evidence supplied to the reviewer."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize the complete captured evidence deterministically."""
        return json.dumps(
            self.evidence_payload(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReviewCandidateV1":
        """Restore persisted evidence without silently normalizing its identity."""
        if payload.get("contract") != _CANDIDATE_CONTRACT:
            raise ValueError(f"Review candidate contract must be {_CANDIDATE_CONTRACT!r}")
        candidate = cls(
            repository=str(payload.get("repository") or ""),
            base_commit=str(payload.get("base_commit") or ""),
            head_commit=str(payload.get("head_commit") or ""),
            accepted_scope=tuple(str(item) for item in payload.get("accepted_scope") or ()),
            tracked_patch=str(payload.get("tracked_patch") or ""),
            untracked_files=tuple(
                ReviewUntrackedFileV1(**item) for item in payload.get("untracked_files") or ()
            ),
            candidate_id=str(payload.get("candidate_id") or ""),
            tracked_patch_sha256=str(payload.get("tracked_patch_sha256") or ""),
        )
        canonical = json.dumps(
            _identity_payload(candidate), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        expected = "sha256:" + hashlib.sha256(canonical).hexdigest()
        if candidate.candidate_id != expected:
            raise ValueError("Persisted review candidate identity is invalid")
        return candidate


@dataclass(frozen=True)
class NativeReviewResultV1:
    candidate_id: str
    runtime_status: str
    exit_reason: str
    judgment: str
    coverage: tuple[str, ...]
    actual_model: str
    summary: str
    contract: str = field(default=_RESULT_CONTRACT, init=False)
    grants_authority: bool = field(default=False, init=False)
    requires_existing_authority: bool = field(default=True, init=False)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, candidate_id: str = "",
    ) -> "NativeReviewResultV1":
        """Restore one runtime-authored result without weakening fail-closed invariants."""
        if payload.get("contract") != _RESULT_CONTRACT:
            raise ValueError(f"Native review result contract must be {_RESULT_CONTRACT!r}")
        bound_id = payload.get("candidate_id")
        if not isinstance(bound_id, str) or not bound_id.startswith("sha256:"):
            raise ValueError("Native review result has an invalid candidate_id")
        if candidate_id and bound_id != candidate_id:
            raise ValueError("Native review result candidate_id does not match the captured candidate")
        runtime_status = payload.get("runtime_status")
        exit_reason = payload.get("exit_reason")
        judgment = payload.get("judgment")
        coverage = payload.get("coverage")
        actual_model = payload.get("actual_model")
        summary = payload.get("summary")
        if not all(isinstance(value, str) for value in (
            runtime_status, exit_reason, judgment, actual_model, summary,
        )):
            raise ValueError("Native review result contains non-string scalar fields")
        if judgment not in _JUDGMENTS | {"unknown"}:
            raise ValueError("Native review result has an invalid judgment")
        if not isinstance(coverage, (list, tuple)) or not all(isinstance(item, str) for item in coverage):
            raise ValueError("Native review result coverage must be an array of strings")
        if judgment in _JUDGMENTS and (runtime_status != "completed" or exit_reason != "completed"):
            raise ValueError("Only a fully completed runtime may carry a review judgment")
        if payload.get("grants_authority") is not False or payload.get("requires_existing_authority") is not True:
            raise ValueError("Native review result cannot grant action authority")
        return cls(
            candidate_id=bound_id, runtime_status=str(runtime_status), exit_reason=str(exit_reason),
            judgment=str(judgment), coverage=tuple(coverage), actual_model=str(actual_model), summary=str(summary),
        )

    @classmethod
    def from_delegation_entry(
        cls, entry: Mapping[str, Any], candidate: ReviewCandidateV1
    ) -> "NativeReviewResultV1":
        """Build a result from runtime-owned fields plus model judgment.

        Runtime status and actual model always come from the delegation entry,
        never from model-authored JSON. A non-completed/invalid run cannot
        become an approval merely because its summary says so.
        """
        require_fresh_candidate(candidate)
        from tools.delegation_output_schema import extract_json_candidate

        raw_summary = str(entry.get("summary") or "")
        try:
            authored = json.loads(extract_json_candidate(raw_summary))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Native review summary is not valid JSON: {exc}") from exc
        if not isinstance(authored, dict):
            raise ValueError("Native review summary must be a JSON object")
        if authored.get("contract") != _JUDGMENT_CONTRACT:
            raise ValueError(f"Native review contract must be {_JUDGMENT_CONTRACT!r}")
        if authored.get("candidate_id") != candidate.candidate_id:
            raise ValueError("Native review candidate_id does not match the captured candidate")

        runtime_status = str(entry.get("status") or "failed")
        schema_valid = entry.get("schema_valid") is True
        authored_judgment = str(authored.get("judgment") or "")
        judgment = (
            authored_judgment
            if (
                runtime_status == "completed"
                and str(entry.get("exit_reason") or "") == "completed"
                and entry.get("truncated") is not True
                and schema_valid
                and authored_judgment in _JUDGMENTS
            )
            else "unknown"
        )
        raw_coverage = authored.get("coverage")
        coverage = tuple(str(item) for item in raw_coverage) if isinstance(raw_coverage, list) else ()
        actual_model = str(entry.get("model") or "")
        provider = str(entry.get("provider") or "").strip()
        if provider and actual_model:
            actual_model = f"{provider}/{actual_model}"
        return cls(
            candidate_id=candidate.candidate_id,
            runtime_status=runtime_status,
            exit_reason=str(entry.get("exit_reason") or ""),
            judgment=judgment,
            coverage=coverage,
            actual_model=actual_model,
            summary=str(authored.get("summary") or ""),
        )


def native_review_output_schema(candidate_id: str) -> dict[str, Any]:
    """Model-authored portion of the native review result.

    The delegation runtime supplies execution state and actual model later.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "contract": {"const": _JUDGMENT_CONTRACT},
            "candidate_id": {"const": candidate_id},
            "judgment": {"enum": sorted(_JUDGMENTS)},
            "coverage": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
        "required": ["contract", "candidate_id", "judgment", "coverage", "summary"],
    }


def native_review_completion_contract(
    candidate: ReviewCandidateV1, *, focus: str = "",
) -> dict[str, Any]:
    """Durable runtime-owned contract carried beside one review delegation."""
    return {
        "kind": _RESULT_CONTRACT,
        "candidate": candidate.evidence_payload(),
        "focus": str(focus or "").strip(),
    }


def _git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", b"")
        rendered = detail.decode("utf-8", "replace").strip() if isinstance(detail, bytes) else str(detail or "")
        raise ValueError(f"Could not capture review candidate with git: {rendered or exc}") from exc


def _normalize_scope(scope: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in scope:
        value = str(raw).strip().replace(os.sep, "/")
        path = Path(value)
        if not value or path.is_absolute() or value in (".", "..") or ".." in path.parts:
            raise ValueError("accepted_scope entries must be repository-relative paths")
        if value.startswith(":"):
            raise ValueError("accepted_scope entries must be literal repository-relative paths")
        normalized.append(value.rstrip("/"))
    if not normalized:
        raise ValueError("accepted_scope must contain at least one explicit path")
    return tuple(sorted(set(normalized)))


def _literal_pathspecs(scope: tuple[str, ...]) -> list[str]:
    return [f":(literal){path}" for path in scope]


def descriptor_capture_supported() -> bool:
    """Whether this platform offers the descriptor-relative operations safe capture needs."""
    return (
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NONBLOCK")
        and all(fn in os.supports_dir_fd for fn in (os.open, os.stat, os.readlink))
        and os.stat in os.supports_follow_symlinks
    )


def _reject_dirty_submodules(root: Path, pathspecs: list[str]) -> None:
    """Fail closed when a checked-out submodule in scope has uncaptured changes.

    Submodule worktrees never appear in the superproject patch or untracked
    listing, so their contents could change without changing the candidate
    identity. Explicit command-line overrides keep this check independent of
    diff.ignoreSubmodules, submodule.<name>.ignore and status.showUntrackedFiles
    at every nesting depth.
    """
    staged = _git(root, "ls-files", "--stage", "-z", "--", *pathspecs)
    for entry in staged.split(b"\0"):
        if not entry.startswith(b"160000 "):
            continue
        relative = entry.split(b"\t", 1)[1].decode("utf-8", "surrogateescape")
        submodule = root / relative
        if not (submodule / ".git").exists():
            continue  # never checked out, so no worktree content can drift
        status = _git(
            submodule, "--no-optional-locks", "status", "--porcelain=v2", "-z",
            "--ignore-submodules=none", "--untracked-files=all",
        )
        if status.strip(b"\0"):
            raise ValueError(
                f"Submodule {relative} has uncommitted changes that review evidence cannot capture"
            )
        _reject_dirty_submodules(submodule, [])


def _evidence_bound_error(relative: str, budget: int) -> ValueError:
    return ValueError(
        f"Untracked review evidence {relative} exceeds the {budget}-byte review evidence bound "
        f"(per-file limit {MAX_UNTRACKED_FILE_BYTES}, aggregate limit {MAX_EVIDENCE_BYTES})"
    )


def _untracked_byte_count(entry: ReviewUntrackedFileV1) -> int:
    encoded = entry.content_base64
    return len(encoded) * 3 // 4 - encoded.count("=")


def _untracked_entry(
    repo: Path, relative: str, *, budget: int | None = None,
) -> ReviewUntrackedFileV1:
    """Capture one untracked path, reading at most ``budget`` content bytes.

    ``budget`` defaults to the per-file limit; callers tracking an aggregate
    budget pass the smaller of the two. The size reported by fstat rejects
    oversized files before any read, and the read itself is bounded so a file
    that grows after that check is still rejected instead of read to EOF.
    """
    from contextlib import ExitStack

    if not descriptor_capture_supported():
        raise ValueError("Safe untracked review capture is unsupported on this platform")
    if budget is None:
        budget = MAX_UNTRACKED_FILE_BYTES
    if budget < 0:
        raise _evidence_bound_error(relative, 0)
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
        raise ValueError("Untracked review evidence must be repository-relative")
    path = repo / relative_path
    if not path.is_absolute():
        raise ValueError("Review repository must be absolute")

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    try:
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent_fd = os.open(path.anchor, flags)
            stack.callback(os.close, parent_fd)
            # Anchor every component, including repository ancestors. No pathname
            # resolution after this walk may follow a replacement symlink.
            for component in path.parts[1:-1]:
                parent_fd = os.open(component, flags, dir_fd=parent_fd)
                stack.callback(os.close, parent_fd)
            leaf = path.name
            info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                content = os.fsencode(os.readlink(leaf, dir_fd=parent_fd))
                if len(content) > budget:
                    raise _evidence_bound_error(relative, budget)
                mode = "120000"
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                stack.callback(os.close, fd)
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(info):
                    raise ValueError("Untracked review evidence changed while opening")
                if opened.st_size > budget:
                    raise _evidence_bound_error(relative, budget)
                # Read one byte past the budget at most, so growth between the
                # fstat check and the read is rejected without buffering the file.
                limit = budget + 1
                chunks: list[bytes] = []
                total = 0
                while total < limit and (chunk := os.read(fd, min(1024 * 1024, limit - total))):
                    chunks.append(chunk)
                    total += len(chunk)
                if total > budget:
                    raise _evidence_bound_error(relative, budget)
                content = b"".join(chunks)
                if identity(os.fstat(fd)) != identity(info):
                    raise ValueError("Untracked review evidence changed while reading")
                mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
            else:
                raise ValueError(f"Unsupported untracked candidate file type: {relative}")
            if identity(os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)) != identity(info):
                raise ValueError("Untracked review evidence changed during capture")
    except OSError as exc:
        raise ValueError("Could not safely capture untracked review evidence") from exc
    return ReviewUntrackedFileV1(
        path=relative,
        mode=mode,
        content_base64=base64.b64encode(content).decode("ascii"),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _identity_payload(candidate: ReviewCandidateV1) -> dict[str, Any]:
    payload = candidate.evidence_payload()
    payload.pop("candidate_id", None)
    # Preserve validation of review_candidate_v1 payloads captured before the
    # exact-byte digest existed. New captures always bind the original patch bytes.
    if not payload.get("tracked_patch_sha256"):
        payload.pop("tracked_patch_sha256", None)
    return payload


def capture_review_candidate(
    repository: str | os.PathLike[str],
    base_revision: str,
    accepted_scope: Iterable[str],
) -> ReviewCandidateV1:
    """Capture one explicit repository/base/scope candidate without mutations."""
    if not str(base_revision or "").strip():
        raise ValueError("base_revision must be explicit")
    requested_repo = Path(repository).expanduser().resolve()
    root = Path(_git(requested_repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    scope = _normalize_scope(accepted_scope)
    base_commit = _git(root, "rev-parse", "--verify", f"{base_revision}^{{commit}}").decode().strip()
    head_commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    pathspecs = _literal_pathspecs(scope)
    _reject_dirty_submodules(root, pathspecs)
    patch_bytes = _git(
        root, "diff", "--binary", "--no-ext-diff", "--no-textconv",
        "--ignore-submodules=none", "--submodule=short", base_commit, "--", *pathspecs,
    )
    remaining = MAX_EVIDENCE_BYTES - len(patch_bytes)
    if remaining < 0:
        raise ValueError(
            f"Tracked review evidence ({len(patch_bytes)} bytes) exceeds the "
            f"{MAX_EVIDENCE_BYTES}-byte aggregate review evidence bound"
        )
    untracked_raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs)
    untracked_paths = sorted(
        item.decode("utf-8", "surrogateescape") for item in untracked_raw.split(b"\0") if item
    )
    entries: list[ReviewUntrackedFileV1] = []
    for path in untracked_paths:
        entry = _untracked_entry(root, path, budget=min(MAX_UNTRACKED_FILE_BYTES, remaining))
        remaining -= _untracked_byte_count(entry)
        entries.append(entry)
    untracked = tuple(entries)
    if not patch_bytes and not untracked:
        raise ValueError("Accepted review scope contains no tracked changes or untracked files")
    try:
        tracked_patch = patch_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Tracked review evidence must be losslessly representable as UTF-8") from exc
    provisional = ReviewCandidateV1(
        repository=str(root),
        base_commit=base_commit,
        head_commit=head_commit,
        accepted_scope=scope,
        tracked_patch=tracked_patch,
        untracked_files=untracked,
        candidate_id="",
        tracked_patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
    )
    canonical = json.dumps(
        _identity_payload(provisional), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return ReviewCandidateV1(
        repository=provisional.repository,
        base_commit=base_commit,
        head_commit=head_commit,
        accepted_scope=scope,
        tracked_patch=provisional.tracked_patch,
        untracked_files=untracked,
        candidate_id="sha256:" + hashlib.sha256(canonical).hexdigest(),
        tracked_patch_sha256=provisional.tracked_patch_sha256,
    )


def require_fresh_candidate(candidate: ReviewCandidateV1) -> ReviewCandidateV1:
    """Re-capture and reject stale/reused results before they are consumed."""
    current = capture_review_candidate(
        candidate.repository, candidate.base_commit, candidate.accepted_scope
    )
    if current.candidate_id != candidate.candidate_id:
        raise ReviewCandidateStale(
            f"Review candidate changed: expected {candidate.candidate_id}, got {current.candidate_id}"
        )
    return current
