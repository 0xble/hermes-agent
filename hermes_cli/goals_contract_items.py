"""Current-contract item discovery and one-time legacy requirement migration.

Facade types and canonical field metadata are late-bound to avoid an import cycle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.goals import GoalState


# Only shorthand names live here; every canonical field automatically supports removal.
_REMOVE_SHORT_ALIASES = {
    "verification": ("check",), "constraints": ("constraint",),
    "boundaries": ("boundary",), "stop_when": ("stop",),
}


def contract_remove_aliases() -> dict[str, str]:
    from hermes_cli.goals import _CONTRACT_FIELDS

    return {alias: field for field in _CONTRACT_FIELDS
            for alias in (*_REMOVE_SHORT_ALIASES.get(field, ()), field)}


def goal_remove_usage() -> str:
    kinds = "|".join((*contract_remove_aliases(), "subgoal"))
    return f"/goal remove <{kinds}> <N>"


def render_removable_contract_items(state: "GoalState | None") -> str:
    from hermes_cli.goals import _CONTRACT_FIELDS, _CONTRACT_LABELS, contract_items

    if state is None:
        return ""
    blocks = []
    fields = [(key, _CONTRACT_LABELS[key], getattr(state.contract, key)) for key in _CONTRACT_FIELDS]
    fields.append(("subgoals", "Subgoals", state.subgoals))
    for key, label, value in fields:
        items = contract_items(key, value)
        if items:
            blocks.append(label + ":\n" + "\n".join(f"- {i}. {item}" for i, item in enumerate(items, 1)))
    return "\n".join(blocks)


def _migrate_legacy_revision_requirements(state: "GoalState") -> bool:
    """Move the old history-backed requirements into current state exactly once.

    Legacy quotes were only evidence for the judge to interpret, not deterministic
    authority. Preserve the earlier bar conservatively as numbered current items:
    /goal remove accepts every restored field. Old objectives become outcome items,
    not a goal replacement, and remain until explicitly removed.
    """
    from hermes_cli.goals import _CONTRACT_FIELDS, contract_items

    migrated = False
    for revision in state.revisions:
        if "authority" in revision or revision.get("legacy_binding_migrated"):
            continue
        before = revision.get("before") or {}
        after = revision.get("after") or {}
        # The old render_revisions_block exposed every changed scalar and dropped subgoal.
        for key in ("goal", *_CONTRACT_FIELDS, "subgoals"):
            if before.get(key) == after.get(key):
                continue
            target = "outcome" if key == "goal" else key
            earlier = contract_items(target, before.get(key))
            if target == "subgoals":
                remaining = contract_items(target, after.get(key))
                earlier = [item for item in earlier if item not in remaining]
                current = contract_items(target, state.subgoals)
            else:
                current = contract_items(target, getattr(state.contract, target))
            restored = list(dict.fromkeys([*current, *earlier]))
            if target == "subgoals":
                state.subgoals = restored
            else:
                setattr(state.contract, target, "\n".join(restored))
        revision["legacy_binding_migrated"] = True
        migrated = True
    return migrated
