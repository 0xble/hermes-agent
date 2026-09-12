#!/usr/bin/env python3
# /// script
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Validate candidate GitHub automation without executing candidate code.

This is run by ``fork-policy.yml`` from the immutable default branch.  Candidate
workflows and composite actions are input data; this validator deliberately has
no history, manifest, registration, or byte-for-byte workflow-baseline rules.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import re
from typing import Any

import yaml

STANDARD_RUNNERS = frozenset({"ubuntu-latest", "windows-latest", "macos-latest"})
# Authorization is staged on the trusted base before any workflow selects these
# labels. No generic self-hosted selection, expressions, or runner groups.
ISOLATED_RUNNER = ["self-hosted", "Linux", "ARM64", "hermes-ci-isolated"]
ISOLATED_JOBS = frozenset({
    "hybrid-pilot.yml.jobs.proof",
    "tests.yml.jobs.test",
    "tests.yml.jobs.e2e",
    "js-tests.yml.jobs.check",
})
SHA_PIN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SECRET_EXPR = re.compile(r"\$\{\{[^}]*\bsecrets\b[^}]*\}\}", re.IGNORECASE)
PUBLISH_COMMAND = re.compile(
    r"(?:\b(?:npm|pnpm|yarn)\b[^\n;&|]*\bpublish\b|"
    r"\bdocker\b[^\n;&|]*\bpush\b|"
    r"\bcargo\b[^\n;&|]*\bpublish\b|"
    r"\bgh\b[^\n;&|]*\brelease\b[^\n;&|]*\b(?:create|upload)\b|"
    r"\btwine\b[^\n;&|]*\bupload\b|"
    r"\b(?:vercel|railway)\b[^\n;&|]*\b(?:deploy|up)\b)",
    re.IGNORECASE,
)
PUBLISH_NAME = re.compile(r"\b(?:deploy|publish)\b", re.IGNORECASE)
TRUSTED_VALIDATOR_INVOCATION = (
    "trusted-policy/scripts/ci/validate_workflow_policy.py "
    "--root candidate --trusted-root trusted-policy"
)
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_UV_ACTION = "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d"

# Names are presentation, but all executable semantics in this privileged job
# are closed over. In particular, only immutable event SHAs select code, the
# candidate is data, and the security validator cannot be skipped.
PRIVILEGED_POLICY_CONTRACT: dict[str, Any] = {
    "on": {"pull_request_target": {"branches": ["main"]}},
    "permissions": {"contents": "read"},
    "concurrency": {
        "group": "fork-policy-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    },
    "jobs": {
        "policy": {
            "runs-on": "ubuntu-latest",
            "timeout-minutes": "5",
            "steps": [
                {
                    "uses": CHECKOUT_ACTION,
                    "with": {
                        "ref": "${{ github.event.pull_request.base.sha }}",
                        "path": "trusted-policy",
                        "persist-credentials": "false",
                    },
                },
                {
                    "uses": CHECKOUT_ACTION,
                    "with": {
                        "repository": "${{ github.event.pull_request.head.repo.full_name }}",
                        "ref": "${{ github.event.pull_request.head.sha }}",
                        "path": "candidate",
                        "persist-credentials": "false",
                        "allow-unsafe-pr-checkout": "true",
                    },
                },
                {
                    "continue-on-error": "true",
                    "run": (
                        "python3 trusted-policy/scripts/validate_maintenance_manifest.py "
                        "candidate/MAINTENANCE.md --json"
                    ),
                },
                {
                    "uses": SETUP_UV_ACTION,
                    "with": {"version": "0.9.28"},
                },
                {"run": f"uv run --script {TRUSTED_VALIDATOR_INVOCATION}"},
            ],
        }
    },
}


def _load(path: Path) -> dict[str, Any]:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: root must be a mapping")
    return data


def _workflow_paths(root: Path) -> list[Path]:
    github = root / ".github"
    directory = github / "workflows"
    if github.is_symlink() or not github.is_dir():
        raise ValueError(".github must be a real directory")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("workflow directory must be a real directory")
    paths = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{path.name}: workflow files must be regular files")
    return paths


def _required_ci_gate(data: dict[str, Any]) -> dict[str, Any]:
    """Return the small immutable correctness-gate surface, excluding labels."""
    jobs = data.get("jobs")
    selected = {
        name: jobs.get(name) if isinstance(jobs, dict) else None
        for name in ("smoke", "result")
    }
    return _without_cosmetic_names({
        "on": data.get("on"), "jobs": selected,
        "defaults": data.get("defaults"), "env": data.get("env"),
    })


def _permission_errors(value: Any, location: str) -> list[str]:
    if not isinstance(value, dict) or not value:
        return [f"{location}: permissions must be an explicit read-only mapping"]
    errors: list[str] = []
    for scope, access in value.items():
        if str(access) != "read":
            errors.append(f"{location}.{scope}: {access!r} permission is forbidden")
    return errors


def _validate_value(node: Any, location: str, errors: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{location}.{key}"
            if key == "permissions":
                errors.extend(_permission_errors(value, child))
            elif key in {"secrets", "environment"}:
                errors.append(f"{child}: secrets and deployment environments are forbidden")
            _validate_value(value, child, errors)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _validate_value(value, f"{location}[{index}]", errors)
    elif isinstance(node, str) and SECRET_EXPR.search(node):
        errors.append(f"{location}: secret references are forbidden")


def _validate_uses(
    value: Any,
    location: str,
    root: Path,
    errors: list[str],
    trusted_actions: frozenset[str],
    *,
    reusable: bool = False,
) -> None:
    uses = str(value)
    if uses.startswith("./"):
        allowed = "./.github/workflows/" if reusable else "./.github/actions/"
        allowed_root = root / allowed[2:-1]
        local = root / uses[2:]
        expected_file = reusable
        valid = local.is_file() if expected_file else local.is_dir()
        try:
            resolved_allowed = allowed_root.resolve(strict=True)
            resolved_local = local.resolve(strict=True)
            resolved_local.relative_to(resolved_allowed)
            resolved_local.relative_to(root.resolve(strict=True))
            lexical = Path(uses[2:])
            if ".." in lexical.parts:
                raise ValueError("local reference traverses its trusted root")
            cursor = root
            has_symlink = False
            for part in lexical.parts:
                cursor /= part
                if cursor.is_symlink():
                    has_symlink = True
                    break
        except (FileNotFoundError, RuntimeError, ValueError):
            has_symlink = True
        if not uses.startswith(allowed) or not valid or has_symlink:
            kind = "workflow file" if reusable else "action directory"
            errors.append(f"{location}: local {kind} must be real and under {allowed}")
        return
    if "@" not in uses:
        errors.append(f"{location}: external actions must be pinned to a full commit SHA")
        return
    _owner, revision = uses.rsplit("@", 1)
    if not SHA_PIN.fullmatch(revision):
        errors.append(f"{location}: external actions must be pinned to a full commit SHA")
    elif uses not in trusted_actions:
        errors.append(f"{location}: external actions must be approved by the trusted base policy")


def _validate_job(
    job: dict[str, Any], location: str, root: Path, errors: list[str], trusted_actions: frozenset[str]
) -> None:
    if "permissions" in job:
        errors.extend(_permission_errors(job["permissions"], f"{location}.permissions"))
    if "uses" in job:
        _validate_uses(job["uses"], f"{location}.uses", root, errors, trusted_actions, reusable=True)
    runner = job.get("runs-on")
    if runner == ISOLATED_RUNNER and location in ISOLATED_JOBS:
        pass
    elif runner == "${{ matrix.runner }}":
        matrix = ((job.get("strategy") or {}).get("matrix") or {}) if isinstance(job.get("strategy"), dict) else {}
        include = matrix.get("include") if isinstance(matrix, dict) else None
        runners = [item.get("runner") for item in include] if isinstance(include, list) and all(isinstance(item, dict) for item in include) else []
        # Axes create jobs independently of include rows. Only the enumerated
        # include-only form has complete, statically validated runner selection.
        include_only = isinstance(matrix, dict) and set(matrix) <= {"include", "exclude"}
        if not include_only or not runners or any(runner not in STANDARD_RUNNERS for runner in runners):
            errors.append(f"{location}.runs-on: only GitHub-hosted standard runners are allowed")
    elif runner is not None and str(runner) not in STANDARD_RUNNERS:
        errors.append(f"{location}.runs-on: only GitHub-hosted standard runners are allowed")
    steps = job.get("steps", [])
    if steps is not None and not isinstance(steps, list):
        errors.append(f"{location}.steps: must be a list")
        return
    for index, step in enumerate(steps or []):
        if not isinstance(step, dict):
            errors.append(f"{location}.steps[{index}]: must be a mapping")
            continue
        step_location = f"{location}.steps[{index}]"
        if "uses" in step:
            _validate_uses(step["uses"], f"{step_location}.uses", root, errors, trusted_actions)
        run = str(step.get("run", ""))
        if PUBLISH_COMMAND.search(run.replace("\\\n", " ")) or PUBLISH_NAME.search(str(step.get("name", ""))):
            errors.append(f"{step_location}: deployment or publish execution is forbidden")


def _validate_local_actions(root: Path, errors: list[str], trusted_actions: frozenset[str]) -> None:
    actions = root / ".github" / "actions"
    if not actions.exists():
        return
    if actions.is_symlink() or not actions.is_dir():
        errors.append(".github/actions must be a real directory")
        return
    for definition in sorted(actions.rglob("action.y*ml")):
        if definition.is_symlink() or not definition.is_file():
            errors.append(f"{definition.relative_to(root)}: action definitions must be regular files")
            continue
        try:
            data = _load(definition)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        runs = data.get("runs")
        if not isinstance(runs, dict) or runs.get("using") != "composite":
            errors.append(f"{definition.relative_to(root)}: only composite local actions are allowed")
            continue
        _validate_value(data, str(definition.relative_to(root)), errors)
        for index, step in enumerate(runs.get("steps", []) if isinstance(runs.get("steps"), list) else []):
            if not isinstance(step, dict):
                errors.append(f"{definition.relative_to(root)}.runs.steps[{index}]: must be a mapping")
                continue
            if "uses" in step:
                _validate_uses(step["uses"], f"{definition.relative_to(root)}.runs.steps[{index}].uses", root, errors, trusted_actions)
            run = str(step.get("run", ""))
            if PUBLISH_COMMAND.search(run.replace("\\\n", " ")):
                errors.append(f"{definition.relative_to(root)}.runs.steps[{index}]: deployment or publish execution is forbidden")


def _validate_privileged_policy(data: dict[str, Any], name: str, errors: list[str]) -> None:
    triggers = data.get("on")
    if name != "fork-policy.yml":
        has_privileged_trigger = (
            triggers == "pull_request_target"
            or isinstance(triggers, list) and "pull_request_target" in triggers
            or isinstance(triggers, dict) and "pull_request_target" in triggers
        )
        if has_privileged_trigger:
            errors.append(f"{name}: pull_request_target is reserved for trusted fork policy")
        return
    semantic = _without_cosmetic_names(data)
    if semantic != PRIVILEGED_POLICY_CONTRACT:
        errors.append("fork-policy.yml: privileged execution contract differs from trusted policy")


def _validate_required_ci_gate(data: dict[str, Any], name: str, errors: list[str]) -> None:
    """Keep the mandatory PR correctness gate from being self-attested away."""
    if name != "ci.yaml":
        return
    if _required_ci_gate(data) != REQUIRED_CI_GATE_CONTRACT:
        errors.append("ci.yaml: mandatory smoke/result correctness gate differs from trusted policy")


def _without_cosmetic_names(data: dict[str, Any]) -> dict[str, Any]:
    """Remove only workflow/job/step labels, never action inputs named ``name``."""
    semantic = deepcopy(data)
    semantic.pop("name", None)
    jobs = semantic.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            job.pop("name", None)
            steps = job.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict):
                        step.pop("name", None)
    return semantic


REQUIRED_CI_GATE_CONTRACT = _required_ci_gate(
    _load(Path(__file__).resolve().parents[2] / ".github/workflows/ci.yaml")
)


def _external_actions(root: Path) -> frozenset[str]:
    """Read only trusted workflow/action declarations to build the action allowlist."""
    actions: set[str] = set()
    try:
        paths = _workflow_paths(root)
    except ValueError:
        return frozenset()
    definitions = [*paths, *(root / ".github" / "actions").rglob("action.y*ml")] if (root / ".github" / "actions").is_dir() else paths
    for path in definitions:
        try:
            data = _load(path)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                uses = node.get("uses")
                if isinstance(uses, str) and not uses.startswith("./"):
                    actions.add(uses)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return frozenset(actions)


def validate(root: Path, trusted_root: Path | None = None) -> list[str]:
    """Return security findings for candidate workflow/action data.

    ``trusted_root`` is intentionally accepted for the stable parent contract;
    policy code is selected by the caller from that immutable checkout, not by
    importing or executing anything below ``root``.
    """
    errors: list[str] = []
    trusted_actions = _external_actions(trusted_root or root)
    try:
        paths = _workflow_paths(root)
    except ValueError as exc:
        return [str(exc)]
    present = {path.name for path in paths}
    for required in ("ci.yaml", "fork-policy.yml"):
        if required not in present:
            errors.append(f"{required}: mandatory workflow is missing")
    for path in paths:
        try:
            data = _load(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        location = path.name
        if "permissions" not in data:
            errors.append(f"{location}: explicit top-level read-only permissions are required")
        else:
            errors.extend(_permission_errors(data["permissions"], f"{location}.permissions"))
        _validate_value(data, location, errors)
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            errors.append(f"{location}: jobs must be a mapping")
        else:
            for job_name, job in jobs.items():
                if not isinstance(job, dict):
                    errors.append(f"{location}.jobs.{job_name}: must be a mapping")
                    continue
                _validate_job(job, f"{location}.jobs.{job_name}", root, errors, trusted_actions)
        _validate_privileged_policy(data, location, errors)
        _validate_required_ci_gate(data, location, errors)
    _validate_local_actions(root, errors, trusted_actions)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--trusted-root", type=Path)
    args = parser.parse_args()
    errors = validate(args.root, args.trusted_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("workflow security policy OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
