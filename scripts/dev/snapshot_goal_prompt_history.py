#!/usr/bin/env python3
"""Regenerate the frozen goal-prompt wording registry.

The persisted transcript has no formatter provenance, so the synthetic-prompt classifier must keep
recognizing every goal continuation wording that could have been written by a shipped Hermes. This
script walks the goal formatter history reachable from ``origin/main`` and the checked-out branch,
partially evaluates the literal formatter assignments, and writes the deduplicated registry module.
It also includes new uncommitted wordings, so the current-formatter guard can pass before a commit.

Run from any checkout with the desired branch checked out::

    python scripts/dev/snapshot_goal_prompt_history.py

The output is deliberately a Python data module: importing it has no Git or filesystem side effects.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
from collections import defaultdict
from pathlib import Path
from pprint import pformat
from string import Formatter
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUTPUT = REPO / "hermes_cli" / "goals_prompt_registry.py"

# These names are the formatter boundaries used by agent.synthetic_prompt. The legacy aliases are
# included because the current goals module keeps them in a separate compatibility module.
FORMATTER_NAMES: dict[str, tuple[str, ...]] = {
    "autonomy": ("AUTONOMY_CONTINUATION_TEMPLATE",),
    "plain": ("CONTINUATION_PROMPT_TEMPLATE", "_LEGACY_CONTINUATION_PROMPT_TEMPLATE"),
    "contract": (
        "CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE",
        "_LEGACY_CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE",
        "_PRE_531_ADDITIVE_CONTRACT_TEMPLATE",
    ),
    "subgoals": (
        "CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE",
        "_LEGACY_CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE",
    ),
    "gate_failed": ("CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE",),
    "revisions": (
        "CONTINUATION_REVISIONS_TEMPLATE",
        "_LEGACY_CONTINUATION_REVISIONS_TEMPLATE",
    ),
    "barrier_lift": ("GOAL_WAIT_LIFTED_NOTE_OPEN",),
}

SOURCE_FILES = ("hermes_cli/goals.py", "hermes_cli/goals_legacy_prompts.py", "hermes_cli/goals_autonomy.py")
FORMATTER_FIELDS = {
    "autonomy": {"goal", "contract_block", "subgoals_block"},
    "plain": {"goal"},
    "contract": {"goal", "contract_block"},
    "subgoals": {"goal", "subgoals_block"},
    "gate_failed": {"goal", "command", "attempt", "max_retries", "exit_code", "output"},
    "revisions": {"revision_lines"},
    "barrier_lift": set(),
}


def _barrier_templates(source: str, values: dict[str, Any]) -> tuple[str, ...]:
    """Freeze every returned barrier note, replacing runtime expressions by format fields."""
    templates: list[str] = []
    for function in ast.parse(source).body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name != "_barrier_lift_note":
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            if isinstance(node.value, ast.Constant):
                rendered = node.value.value
            elif isinstance(node.value, ast.JoinedStr):
                parts: list[str] = []
                fields: dict[str, str] = {}
                for part in node.value.values:
                    if isinstance(part, ast.Constant):
                        parts.append(str(part.value))
                    elif isinstance(part, ast.FormattedValue):
                        value = _literal(part.value, values)
                        if isinstance(value, str):
                            parts.append(value)
                        else:
                            expression = ast.unparse(part.value)
                            field = fields.setdefault(expression, f"note_field_{len(fields)}")
                            parts.append("{" + field + "}")
                rendered = "".join(parts)
            else:
                raise ValueError("unsupported barrier-lift formatter: " + ast.unparse(node.value))
            if isinstance(rendered, str) and rendered.startswith("[Goal wait lifted: "):
                templates.append(rendered)
    return tuple(dict.fromkeys(templates))


class _Unknown:
    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return "{" + self.name + "}"


def _literal(node: ast.AST, values: dict[str, Any]) -> Any:
    """Evaluate the literal subset used by the formatter constants."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id, _Unknown(node.id))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left, values), _literal(node.right, values)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        return _Unknown("expression")
    if isinstance(node, ast.JoinedStr):
        result: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                result.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                rendered = _literal(value.value, values)
                result.append(str(rendered))
        return "".join(result)
    if isinstance(node, ast.Tuple):
        return tuple(_literal(element, values) for element in node.elts)
    if isinstance(node, ast.List):
        return [_literal(element, values) for element in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal(node.operand, values)
        return -value if isinstance(value, (int, float)) else _Unknown("expression")
    return _Unknown(ast.unparse(node) if hasattr(ast, "unparse") else "expression")


def _assignments(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    # Imported constants are stable across the history we scan. Supplying them here lets the
    # evaluator resolve an f-string without importing a historical Hermes module and its deps.
    values: dict[str, Any] = {"SILENCE_MARKER": "[SILENT]"}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if not names:
            continue
        value_node = node.value
        if value_node is None:
            continue
        value = _literal(value_node, values)
        for name in names:
            values[name] = value
    return values


def _git(*args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=REPO, text=True, encoding="utf-8", errors="replace",
                                   stderr=subprocess.DEVNULL, timeout=90)


def _commits() -> list[str]:
    # The revision set is the union of the current branch and origin/main, not unrelated local
    # branches. Keep Git's newest-first order so the generated source metadata is deterministic.
    return _git("log", "--full-history", "--format=%H", "origin/main", "HEAD", "--", *SOURCE_FILES).splitlines()


def _collect_source(registry, commit: str, source_file: str, source: str, *, new_only: bool = False) -> None:
    values = _assignments(source)
    for kind, names in FORMATTER_NAMES.items():
        for name in names:
            value = values.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"could not evaluate {commit}:{source_file}:{name}")
            fields = {field for _, field, _, _ in Formatter().parse(value) if field}
            if fields != FORMATTER_FIELDS[kind]:
                raise ValueError(f"unresolved formatter fields in {commit}:{source_file}:{name}: {fields}")
            if new_only and value in registry[kind]:
                continue
            if commit not in registry[kind][value]:
                registry[kind][value].append(commit)
    for template in _barrier_templates(source, values):
        if new_only and template in registry["barrier_lift_note"]:
            continue
        if commit not in registry["barrier_lift_note"][template]:
            registry["barrier_lift_note"][template].append(commit)


def _snapshot() -> dict[str, dict[str, list[str]]]:
    registry: dict[str, dict[str, list[str]]] = {
        kind: defaultdict(list) for kind in (*FORMATTER_NAMES, "barrier_lift_note")
    }
    for commit in _commits():
        for source_file in SOURCE_FILES:
            try:
                source = _git("show", f"{commit}:{source_file}")
            except subprocess.CalledProcessError:
                continue
            _collect_source(registry, commit, source_file, source)
    # Include new uncommitted wording so regeneration can satisfy the guard BEFORE committing it.
    # It gets an honest working-tree label, never the SHA of a commit that lacks that wording.
    for source_file in SOURCE_FILES:
        path = REPO / source_file
        if path.exists():
            _collect_source(registry, "working-tree", source_file, path.read_text(encoding="utf-8-sig"), new_only=True)
    # Shipped wordings may come from retired branches no longer reachable from main.
    # Keep their frozen provenance rather than turning those persisted turns into human input.
    if OUTPUT.exists():
        for node in ast.parse(OUTPUT.read_text(encoding="utf-8-sig")).body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "GOAL_PROMPT_REGISTRY":
                for row in ast.literal_eval(node.value):
                    registry[row["formatter"]][row["template"]] = list(row["source_commits"])
    return registry


def _render(registry: dict[str, dict[str, list[str]]]) -> str:
    rows: list[dict[str, Any]] = []
    for kind in registry:
        entries = sorted(registry[kind].items(), key=lambda item: (len(item[0]), item[0]))
        for template, commits in entries:
            rows.append({
                "formatter": kind,
                "template": template,
                "source_commits": tuple(dict.fromkeys((commits[0], commits[-1]))),
            })
    scanned_commits = tuple(dict.fromkeys(
        commit for entries in registry.values() for commits in entries.values() for commit in commits
        if commit != "working-tree"
    ))
    if OUTPUT.exists():
        for node in ast.parse(OUTPUT.read_text(encoding="utf-8-sig")).body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "SCANNED_GOAL_PROMPT_COMMITS":
                scanned_commits = tuple(dict.fromkeys((*ast.literal_eval(node.value), *scanned_commits)))
    grouped_counts = {kind: len(entries) for kind, entries in registry.items()}
    body = pformat(tuple(rows), width=120, sort_dicts=False)
    return f'''"""Generated goal formatter wording registry; do not edit by hand.\n\nRegenerate with:\n    python scripts/dev/snapshot_goal_prompt_history.py\n"""\n\nfrom typing import Final, TypedDict\n\n\nclass GoalPromptEntry(TypedDict):\n    formatter: str\n    template: str\n    source_commits: tuple[str, ...]\n\n\nGOAL_PROMPT_REGISTRY: Final[tuple[GoalPromptEntry, ...]] = {body}\n\n# Current formatter names are the guard-test contract: a wording change must first be added to\n# the generated history snapshot before the classifier can be considered complete.\nCURRENT_GOAL_FORMATTER_NAMES: Final[tuple[tuple[str, str], ...]] = (\n    ("autonomy", "AUTONOMY_CONTINUATION_TEMPLATE"),\n    ("plain", "CONTINUATION_PROMPT_TEMPLATE"),\n    ("contract", "CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE"),\n    ("subgoals", "CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE"),\n    ("gate_failed", "CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE"),\n    ("barrier_lift", "GOAL_WAIT_LIFTED_NOTE_OPEN"),\n)\n\nGOAL_PROMPT_REGISTRY_COUNTS: Final[dict[str, int]] = {grouped_counts!r}\n\n# All historical goal formatter versions contributing to the frozen registry. Individual rows\n# name their newest and oldest source commits instead of repeating this complete list per row.\nSCANNED_GOAL_PROMPT_COMMITS: Final[tuple[str, ...]] = {pformat(scanned_commits, width=120)}\n'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    registry = _snapshot()
    if not any(registry.values()):
        raise SystemExit("no goal formatter assignments found in origin/main or HEAD")
    output = args.output if args.output.is_absolute() else REPO / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render(registry), encoding="utf-8")
    total = sum(len(entries) for entries in registry.values())
    print(f"wrote {output} ({total} distinct entries; "
          + ", ".join(f"{kind}={len(registry[kind])}" for kind in registry) + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
