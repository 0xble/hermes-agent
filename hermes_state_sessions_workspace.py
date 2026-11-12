"""Workspace grouping for persisted sessions."""

from typing import Any, Optional

def workspace_key(row: dict[str, Any]) -> Optional[str]:
    """Workspace grouping key: git repo root, else cwd, else None (branch excluded: a checkout must not
    fragment history)."""
    return (row.get("git_repo_root") or "").strip() or (row.get("cwd") or "").strip() or None
