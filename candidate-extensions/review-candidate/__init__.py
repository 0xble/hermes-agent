"""Candidate-bound review tool for the Hermes Agent Next build."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = {
    "name": "review_candidate",
    "description": "Review an exact Git candidate once. Supply repository, base_sha, head_sha, and an optional repository-relative scope. The tool records a durable receipt and never edits the candidate.",
    "parameters": {
        "type": "object",
        "properties": {
            "repository": {"type": "string", "description": "Absolute Git repository path."},
            "base_sha": {"type": "string", "description": "Exact review base commit."},
            "head_sha": {"type": "string", "description": "Exact candidate commit."},
            "scope": {"type": "array", "items": {"type": "string"}, "description": "Optional repository-relative paths."},
        },
        "required": ["repository", "base_sha", "head_sha"],
    },
}

# Upper bound on the candidate handed to one review child. A candidate larger than this is
# REFUSED rather than truncated: a silently shortened diff would produce a receipt claiming
# coverage the reviewer never saw.
_MAX_DIFF_BYTES = 2_000_000

# Only a provider-level AVAILABILITY failure may fall back to the secondary reviewer. A child
# that started and then failed, timed out, or returned an unknown status is recorded as
# "not reviewed" — re-running it on another model would review a different thing and is not
# what the delivery gate promises.
_AVAILABILITY_PATTERN = re.compile(
    r"\b(401|403|429|50[0-9]|auth|authentication|unauthorized|credential|quota|rate.?limit|"
    r"overloaded|unavailable|capacity|no such model|model not found|could not start|failed to start|"
    r"connection|network|dns)\b",
    re.IGNORECASE,
)
_NON_AVAILABILITY_PATTERN = re.compile(r"\b(timeout|timed out|unknown|interrupted|cancell?ed)\b", re.IGNORECASE)

_FALLBACK_PROVIDER = "anthropic"
_FALLBACK_MODEL = "claude-opus-5"


def _json(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _receipt_path(head_sha: str = "") -> Path:
    """One receipt per candidate head, so an earlier candidate's coverage is never overwritten."""
    from hermes_constants import get_hermes_home
    name = f"{head_sha}.json" if head_sha else "review-candidate.json"
    return get_hermes_home() / "review_receipts" / name


def _existing_receipt(path: Path, *, base_sha: str, head_sha: str, scope: list[str]) -> dict[str, Any] | None:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if (receipt.get("status") == "reviewed" and receipt.get("base_sha") == base_sha
            and receipt.get("head_sha") == head_sha and receipt.get("scope") == scope):
        return receipt
    return None


def _is_availability_failure(error: Any) -> bool:
    """True only for provider-level failures that mean the reviewer never ran."""
    text = str(error or "")
    if _NON_AVAILABILITY_PATTERN.search(text):
        return False
    return bool(_AVAILABILITY_PATTERN.search(text))


def _structured_verdict(result: dict[str, Any]) -> dict[str, Any]:
    """The reviewer's verdict as data, parsed out of the child's final message.

    The delegate result wraps the child's prose in a summary string; a receipt whose verdict can
    only be found by a human reading that prose is not reusable by later delivery stages. A fenced
    ```json block is preferred, then any bare JSON object with a ``verdict`` key. When nothing
    parses, the verdict is ``unparsed`` and delivery must treat it as changes_requested.
    """
    tasks = result.get("results") if isinstance(result.get("results"), list) else [result]
    text = ""
    for task in tasks or []:
        if isinstance(task, dict):
            text = str(task.get("summary") or task.get("output") or task.get("content") or "")
            if text:
                break
    candidates: list[str] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(match.group(1))
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:text.rfind("}") + 1])
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and "verdict" in parsed:
            parsed.setdefault("findings", [])
            parsed.setdefault("summary", "")
            return parsed
    return {"verdict": "unparsed", "findings": [], "summary": text[:2000]}


def _spawn_review(context: str, head: str, parent: Any, credentials: dict[str, Any] | None) -> dict[str, Any]:
    """Run one synchronous review child and return its parsed result.

    ``background=False`` is deliberate and is the DIRECT Python-caller contract: the model-facing
    registry path forces background for top-level delegations, but this tool must have the child's
    findings in-band to write a receipt at all.
    """
    from tools.delegate_tool import delegate_task
    raw = delegate_task(
        goal=f"Review candidate {head[:12]}", context=context,
        background=False, parent_agent=parent, credentials_cfg=credentials,
    )
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"error": f"review dispatch returned unparsable output: {str(raw)[:400]}"}


def review_candidate(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            return _json(success=False, status="not_reviewed", error_code="parent_only",
                         error="review_candidate is available only to the owning parent session")
        repo = Path(str(args.get("repository") or "")).expanduser().resolve()
        base = str(args.get("base_sha") or "").strip()
        head = str(args.get("head_sha") or "").strip()
        scope = args.get("scope") or []
        if not repo.is_dir() or not base or not head or not isinstance(scope, list) or any(not isinstance(p, str) for p in scope):
            return _json(success=False, status="not_reviewed", error_code="invalid_candidate",
                         error="repository, base_sha, head_sha, and a string scope are required")
        base_resolved = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        head_resolved = _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
        if base_resolved == head_resolved:
            return _json(success=False, status="not_reviewed", error_code="empty_candidate",
                         error="base and head must differ")
        diff = _git(repo, "diff", "--no-ext-diff", "--unified=3", base_resolved, head_resolved, "--", *scope)
        if not diff:
            return _json(success=False, status="not_reviewed", error_code="empty_scope",
                         error="candidate scope has no diff")
        changed = [line for line in _git(repo, "diff", "--name-only", base_resolved, head_resolved,
                                         "--", *scope).splitlines() if line]
        covers = sorted(set(changed))

        receipt_path = _receipt_path(head_resolved)
        existing = _existing_receipt(receipt_path, base_sha=base_resolved, head_sha=head_resolved, scope=covers)
        if existing is not None:
            return _json(success=True, reused=True, **existing)

        if len(diff.encode("utf-8")) > _MAX_DIFF_BYTES:
            return _json(success=False, status="not_reviewed", error_code="diff_too_large",
                         base_sha=base_resolved, head_sha=head_resolved,
                         error="candidate diff exceeds the review context limit; split the candidate")

        context = (
            "You are the independent reviewer for this exact candidate. Do not edit files, create "
            "commits, or call review_candidate (it is parent-only and you are the reviewer it spawned). "
            "Inspect the repository read-only and run relevant tests when a terminal is available; "
            "if it is not, say so and review the diff statically. Your FINAL message must be a single "
            "fenced ```json block with keys verdict (approve or changes_requested), findings (array of "
            "objects with severity, path, line, message), and summary. No prose outside the block.\n\n"
            f"Repository: {repo}\nBase: {base_resolved}\nHead: {head_resolved}\n"
            f"Covered paths: {json.dumps(covers)}\nFull diff:\n{diff}"
        )

        from agent.review_engine import _load_review_credentials_cfg
        from agent.subagent_lifecycle import get_active_subagent_parent
        parent = get_active_subagent_parent()
        if parent is None:
            return _json(success=False, status="not_reviewed", error_code="parent_context_unavailable",
                         base_sha=base_resolved, head_sha=head_resolved,
                         error="review child requires an active parent agent")
        credentials = _load_review_credentials_cfg()
        primary_model = str((credentials or {}).get("model") or "")

        result = _spawn_review(context, head_resolved, parent, credentials)
        reviewer_model, fallback_reason = primary_model, ""
        if result.get("error"):
            if not _is_availability_failure(result["error"]):
                # The reviewer ran and did not finish cleanly. Another model is NOT a substitute.
                return _json(success=False, status="not_reviewed", error_code="review_incomplete",
                             base_sha=base_resolved, head_sha=head_resolved, reviewer_model=primary_model,
                             error=str(result["error"]))
            fallback_reason = str(result["error"])[:500]
            fallback = dict(credentials or {})
            fallback["provider"], fallback["model"] = _FALLBACK_PROVIDER, _FALLBACK_MODEL
            result = _spawn_review(context, head_resolved, parent, fallback)
            reviewer_model = _FALLBACK_MODEL
            if result.get("error"):
                return _json(success=False, status="not_reviewed", error_code="review_unavailable",
                             base_sha=base_resolved, head_sha=head_resolved,
                             error=f"primary unavailable ({fallback_reason}); fallback failed: {result['error']}")

        receipt = {
            "status": "reviewed",
            "repository": str(repo),
            "base_sha": base_resolved,
            "head_sha": head_resolved,
            "scope": covers,
            "covers": covers,
            "reviewer_model": str(result.get("review_model") or reviewer_model),
            "fallback_reason": fallback_reason,
            "result": _structured_verdict(result),
            "raw_child_result": result.get("results", result),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=receipt_path.parent, delete=False, encoding="utf-8") as tmp:
            json.dump(receipt, tmp, sort_keys=True, indent=2)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, receipt_path)
        return _json(success=True, **receipt)
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        return _json(success=False, status="not_reviewed", error_code="candidate_read_failed", error=str(exc))
    except Exception as exc:
        return _json(success=False, status="not_reviewed", error_code="review_failed", error=str(exc))


def register(ctx: Any) -> None:
    ctx.register_tool(name="review_candidate", toolset="review_candidate",
                      schema=_SCHEMA, handler=review_candidate)
