"""Candidate-bound review tool for the Hermes Agent Next build."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_SCHEMA = {
    "name": "review_candidate",
    "description": (
        "Review an exact Git candidate once. Supply repository, base_sha, head_sha, and",
        "an optional repository-relative scope. The tool records a durable receipt and",
        "never edits the candidate.",
    ),
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


def _json(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _receipt_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "reviews" / "review-candidate.json"


def _existing_receipt(path: Path, *, base_sha: str, head_sha: str, scope: list[str]) -> dict[str, Any] | None:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if (receipt.get("status") == "reviewed" and receipt.get("base_sha") == base_sha
            and receipt.get("head_sha") == head_sha and receipt.get("scope") == scope):
        return receipt
    return None


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
        diff_args = ["diff", "--no-ext-diff", "--unified=3", base_resolved, head_resolved, "--", *scope]
        diff = _git(repo, *diff_args)
        if not diff:
            return _json(success=False, status="not_reviewed", error_code="empty_scope",
                         error="candidate scope has no diff")
        receipt_path = _receipt_path()
        existing = _existing_receipt(receipt_path, base_sha=base_resolved,
                                      head_sha=head_resolved, scope=scope)
        if existing is not None:
            return _json(success=True, reused=True, **existing)
        prompt = (
            "You are reviewing an exact code candidate. Read the supplied diff only. "
            "Return JSON with keys verdict (approve or changes_requested), findings (array of "
            "objects with severity, path, line, and message), and summary. Do not edit files.\n\n"
            f"Repository: {repo}\nBase: {base_resolved}\nHead: {head_resolved}\nDiff:\n{diff[:120000]}"
        )
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("review")
        if client is None or not model:
            return _json(success=False, status="not_reviewed", error_code="review_unavailable",
                         base_sha=base_resolved, head_sha=head_resolved,
                         error="review route is unavailable")
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": "Return only valid JSON."}, {"role": "user", "content": prompt}],
            temperature=0,
        )
        content = response.choices[0].message.content or ""
        try:
            findings = json.loads(content)
        except json.JSONDecodeError:
            findings = {"verdict": "changes_requested", "findings": [{"severity": "high", "message": "Reviewer returned invalid JSON"}], "summary": content[:2000]}
        receipt = {
            "status": "reviewed", "repository": str(repo), "base_sha": base_resolved,
            "head_sha": head_resolved, "scope": scope, "reviewer_model": model, "result": findings,
        }
        path = receipt_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
            json.dump(receipt, tmp, sort_keys=True, indent=2)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
        return _json(success=True, **receipt)
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        return _json(success=False, status="not_reviewed", error_code="candidate_read_failed", error=str(exc))
    except Exception as exc:
        return _json(success=False, status="not_reviewed", error_code="review_failed", error=str(exc))


def register(ctx: Any) -> None:
    ctx.register_tool(name="review_candidate", toolset="review_candidate",
                      schema=_SCHEMA, handler=review_candidate)
