"""Guard externally owned skills and route improvement work to observations.

The generated skill install is a read-only projection of the canonical source.  This
hook is a second, runtime guard for ``skill_manage`` because filesystem permissions
alone do not explain the correct write path to the model.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable


_WRITE_ACTIONS = {"create", "patch", "edit", "delete", "write_file", "remove_file"}


def _operations(args: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(args, dict):
        return ()
    operations = args.get("operations")
    if isinstance(operations, list):
        return (item for item in operations if isinstance(item, dict))
    return (args,)


def _external_skill_names() -> set[str]:
    try:
        from agent.skill_utils import get_external_skills_dirs

        names: set[str] = set()
        for root in get_external_skills_dirs():
            for skill_md in root.rglob("SKILL.md"):
                if skill_md.is_file():
                    names.add(skill_md.parent.name)
        return names
    except Exception:
        # A guard must fail closed when it cannot establish the external roots.
        return {"*"}


def _blocked_skill_names(args: Any) -> list[str]:
    external_names = _external_skill_names()
    blocked: list[str] = []
    for operation in _operations(args):
        action = str(operation.get("action") or "").strip().lower()
        name = str(operation.get("name") or "").strip()
        if action in _WRITE_ACTIONS and name and (name in external_names or "*" in external_names):
            blocked.append(name)
    return list(dict.fromkeys(blocked))


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> dict[str, str] | None:
    if tool_name != "skill_manage":
        return None
    blocked = _blocked_skill_names(args)
    if not blocked:
        return None
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    observations = ", ".join(f"$HERMES_HOME/observations/{name}.md" for name in blocked)
    return {
        "action": "block",
        "message": (
            "canonical-skill-guard refused this skill update because the target is externally owned "
            f"({', '.join(blocked)}). Record the proposed improvement in {observations}; "
            "the weekly curation workflow will review the observation and update the canonical source."
        ),
    }


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
