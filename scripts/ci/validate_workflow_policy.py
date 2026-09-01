#!/usr/bin/env python3
# /// script
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Fail closed on drift in the private mirror's workflow surface."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

CHECKOUT_ACTION = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
SETUP_UV_ACTION = "astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39"
SETUP_UV_ACTION_NEXT = "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d"
SETUP_UV_ACTION_FAMILY = frozenset({SETUP_UV_ACTION, SETUP_UV_ACTION_NEXT})

WORKFLOW_TRIGGERS: dict[str, frozenset[str]] = {
    "ci.yaml": frozenset({"pull_request", "push"}),
    "docs-site-checks.yml": frozenset({"workflow_call"}),
    "docker-lint.yml": frozenset({"workflow_call"}),
    "e2e-desktop.yml": frozenset({"workflow_call", "workflow_dispatch"}),
    "fork-policy.yml": frozenset({"pull_request_target"}),
    "history-check.yml": frozenset({"workflow_call"}),
    "install-e2e-run.yml": frozenset({"workflow_call"}),
    "install-e2e.yml": frozenset({"workflow_dispatch"}),
    "installer-tests.yml": frozenset({"workflow_call"}),
    "js-tests.yml": frozenset({"workflow_call"}),
    "lint.yml": frozenset({"workflow_call"}),
    "lockfile-diff.yml": frozenset({"workflow_call"}),
    "nix.yml": frozenset({"workflow_dispatch"}),
    "osv-scanner.yml": frozenset({"schedule", "workflow_call", "workflow_dispatch"}),
    "rust-tests.yml": frozenset({"workflow_call"}),
    "supply-chain-audit.yml": frozenset({"workflow_call"}),
    "tests-os.yml": frozenset({"workflow_call"}),
    "tests.yml": frozenset({"workflow_call"}),
    "uv-lockfile-check.yml": frozenset({"workflow_call"}),
}

# Event names alone are not sufficient policy: branch filters and schedules
# can silently broaden or narrow when a workflow runs.
WORKFLOW_TRIGGER_CONFIGS: dict[str, dict[str, Any]] = {
    "ci.yaml": {
        "pull_request": "",
        "push": {"branches": ["main"]},
    },
    "fork-policy.yml": {
        "pull_request_target": {"branches": ["main"]},
    },
    "osv-scanner.yml": {
        "workflow_call": "",
        "schedule": [{"cron": "0 9 * * 1"}],
        "workflow_dispatch": "",
    },
}

WORKFLOW_PERMISSIONS: dict[str, dict[str, str]] = {
    name: {"contents": "read"} for name in WORKFLOW_TRIGGERS
}
# Artifact download in the scheduled OSV workflow needs Actions read access.
WORKFLOW_PERMISSIONS["osv-scanner.yml"] = {"actions": "read", "contents": "read"}
# Reusable workflows cannot elevate permissions beyond their caller.
WORKFLOW_PERMISSIONS["ci.yaml"] = {"actions": "read", "contents": "read"}

FORBIDDEN_WORKFLOWS = frozenset(
    {
        "ci-review-comment.yml",
        "contributor-check.yml",
        "deploy-site.yml",
        "docker.yml",
        "infographic-check.yml",
        "js-autofix.yml",
        "label-rerun.yml",
        "publish-e2e-evidence.yml",
        "review-labels.yml",
        "skills-index-freshness.yml",
        "skills-index.yml",
        "windows-venv-e2e.yml",
    }
)

STANDARD_RUNNERS = frozenset({"ubuntu-latest", "windows-latest", "macos-latest"})
# This is the only dynamic runs-on expression. Both the expression and matrix
# values are checked so no other expression can select a runner.
DYNAMIC_RUNNER_ALLOWLIST = {
    "tests-os.yml": {
        "expression": "${{ matrix.runner }}",
        "runners": ("macos-latest", "windows-latest"),
    },
}

# Every current step action and job-level reusable workflow is listed exactly.
# A new reference, including a new local reference, requires a trusted policy
# update before it can pass.
STEP_ACTION_ALLOWLIST = frozenset(
    {
        "./.github/actions/detect-changes",
        "./.github/actions/retry",
        "actions/cache/save@0400d5f644dc74513175e3cd8d07132dd4860809",
        "actions/cache@0400d5f644dc74513175e3cd8d07132dd4860809",
        CHECKOUT_ACTION,
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
        "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020",
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        *SETUP_UV_ACTION_FAMILY,
        "cachix/install-nix-action@630ae543ea3a38a9a4166f03376c02c50f408342",
        "google/osv-scanner-action/osv-scanner-action@9a498708959aeaef5ef730655706c5a1df1edbc2",
        "hadolint/hadolint-action@54c9adbab1582c2ef04b2016b760714a4bfde3cf",
        "ludeeus/action-shellcheck@00cae500b08a931fb5698e11e79bfbd38e612a38",
        "nix-community/cache-nix-action@7df957e333c1e5da7721f60227dbba6d06080569",
    }
)
JOB_REUSABLE_WORKFLOW_ALLOWLIST = frozenset(
    {
        "./.github/workflows/docker-lint.yml",
        "./.github/workflows/docs-site-checks.yml",
        "./.github/workflows/history-check.yml",
        "./.github/workflows/install-e2e-run.yml",
        "./.github/workflows/installer-tests.yml",
        "./.github/workflows/js-tests.yml",
        "./.github/workflows/lint.yml",
        "./.github/workflows/lockfile-diff.yml",
        "./.github/workflows/osv-scanner.yml",
        "./.github/workflows/rust-tests.yml",
        "./.github/workflows/supply-chain-audit.yml",
        "./.github/workflows/tests-os.yml",
        "./.github/workflows/tests.yml",
        "./.github/workflows/uv-lockfile-check.yml",
    }
)

# The privileged event workflow is deliberately exact, not merely constrained.
# It can only run trusted default-branch validators against an immutable
# candidate checkout treated as data.
FORK_POLICY_WORKFLOW: dict[str, Any] = {
    "name": "Trusted fork policy",
    "on": {"pull_request_target": {"branches": ["main"]}},
    "permissions": {"contents": "read"},
    "concurrency": {
        "group": "fork-policy-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    },
    "jobs": {
        "policy": {
            "name": "Validate candidate with trusted policy",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": "5",
            "steps": [
                {
                    "name": "Checkout trusted default-branch policy",
                    "uses": CHECKOUT_ACTION,
                    "with": {
                        "ref": "${{ github.event.repository.default_branch }}",
                        "path": "trusted-policy",
                        "persist-credentials": "false",
                    },
                },
                {
                    "name": "Checkout immutable candidate as data",
                    "uses": CHECKOUT_ACTION,
                    "with": {
                        "repository": "${{ github.event.pull_request.head.repo.full_name }}",
                        "ref": "${{ github.event.pull_request.head.sha }}",
                        "path": "candidate",
                        "fetch-depth": "0",
                        "persist-credentials": "false",
                    },
                },
                {
                    "name": "Fetch canonical upstream history into candidate checkout",
                    "run": (
                        "git -C candidate fetch --no-tags --filter=blob:none "
                        "https://github.com/NousResearch/hermes-agent.git "
                        "refs/heads/main:refs/remotes/canonical-upstream/main"
                    ),
                },
                {
                    "name": "Validate maintained patch history with trusted code",
                    "run": (
                        "python3 trusted-policy/scripts/validate_maintenance_manifest.py "
                        "candidate/MAINTENANCE.md --upstream-ref canonical-upstream/main "
                        "--history-baseline-subject "
                        "'fix(ci): advance maintenance baseline after contributor cleanup'"
                    ),
                },
                {
                    "name": "Install pinned uv for trusted workflow policy",
                    "uses": SETUP_UV_ACTION,
                    "with": {"version": "0.9.28"},
                },
                {
                    "name": "Validate candidate workflow surface with trusted code",
                    "run": (
                        "uv run --script "
                        "trusted-policy/scripts/ci/validate_workflow_policy.py "
                        "--root candidate"
                    ),
                },
            ],
        }
    },
}
FORK_POLICY_WORKFLOW_NEXT: dict[str, Any] = {
    **FORK_POLICY_WORKFLOW,
    "jobs": {
        "policy": {
            **FORK_POLICY_WORKFLOW["jobs"]["policy"],
            "steps": [
                *FORK_POLICY_WORKFLOW["jobs"]["policy"]["steps"][:4],
                {
                    **FORK_POLICY_WORKFLOW["jobs"]["policy"]["steps"][4],
                    "uses": SETUP_UV_ACTION_NEXT,
                },
                *FORK_POLICY_WORKFLOW["jobs"]["policy"]["steps"][5:],
            ],
        }
    },
}

# Match publication commands even when global options or a Rust toolchain pin
# appear between the executable and subcommand. Escaped newlines are folded
# before matching; unescaped shell separators stop a match.
PUBLISH_COMMAND = re.compile(
    r"(?:"
    r"\b(?:npm|pnpm|yarn)\b[^\n;&|]*\bpublish\b|"
    r"\bdocker\b[^\n;&|]*\bpush\b|"
    r"\bcargo\b[^\n;&|]*\bpublish\b|"
    r"\bgh\b[^\n;&|]*\brelease\b[^\n;&|]*\b(?:create|upload)\b|"
    r"\btwine\b[^\n;&|]*\bupload\b|"
    r"\b(?:vercel|railway)\b[^\n;&|]*\b(?:deploy|up)\b"
    r")",
    re.IGNORECASE,
)
PUBLISH_NAME = re.compile(r"\b(?:deploy|publish)\b", re.IGNORECASE)
SECRET_EXPR = re.compile(r"\$\{\{[^}]*\bsecrets\b[^}]*\}\}", re.IGNORECASE)


def _load(path: Path) -> dict[str, Any]:
    try:
        # BaseLoader keeps GitHub's `on` key as a string (YAML 1.2 semantics)
        # while resolving lists, maps, anchors, and aliases.
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: workflow root must be a mapping")
    return data


def _triggers(data: dict[str, Any], path: Path) -> frozenset[str]:
    value = data.get("on")
    if isinstance(value, dict):
        found = frozenset(str(key) for key in value)
    elif isinstance(value, list):
        found = frozenset(str(item) for item in value)
    elif isinstance(value, str):
        found = frozenset({value})
    else:
        raise ValueError(f"{path.name}: 'on' must be a trigger string, list, or mapping")
    if not found:
        raise ValueError(f"{path.name}: no workflow triggers found")
    return found


def _validate_permissions(
    node: Any,
    location: str,
    errors: list[str],
    allowed: dict[str, str],
) -> None:
    if isinstance(node, str):
        errors.append(f"{location}: permissions must be an explicit read-only mapping")
        return
    if not isinstance(node, dict):
        errors.append(f"{location}: permissions must be a read-only mapping")
        return
    for scope, access in node.items():
        scope_text = str(scope)
        if scope_text not in allowed:
            errors.append(f"{location}.{scope_text}: permission scope is not allowed")
        elif str(access) != allowed[scope_text]:
            errors.append(f"{location}.{scope}: permission {access!r} is forbidden")


def _validate_security(
    node: Any,
    location: str,
    errors: list[str],
    allowed_permissions: dict[str, str],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_text = str(key)
            child = f"{location}.{key_text}"
            if key_text == "permissions":
                _validate_permissions(value, child, errors, allowed_permissions)
            elif key_text in {"secrets", "environment"}:
                errors.append(f"{child}: secrets and deployment environments are forbidden")
            _validate_security(value, child, errors, allowed_permissions)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _validate_security(value, f"{location}[{index}]", errors, allowed_permissions)
    elif isinstance(node, str) and SECRET_EXPR.search(node):
        errors.append(f"{location}: secret references are forbidden")


def _contains_publish_command(run: str) -> bool:
    return bool(PUBLISH_COMMAND.search(run.replace("\\\n", " ")))


def _validate_steps(data: dict[str, Any], name: str, errors: list[str]) -> None:
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        errors.append(f"{name}: jobs must be a mapping")
        return
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_uses = job.get("uses")
        if job_uses is not None and str(job_uses) not in JOB_REUSABLE_WORKFLOW_ALLOWLIST:
            errors.append(
                f"{name}.jobs.{job_name}.uses: unapproved reusable workflow reference "
                f"{job_uses!r}"
            )
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            errors.append(f"{name}.jobs.{job_name}.steps: must be a list")
            continue
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            location = f"{name}.jobs.{job_name}.steps[{index}]"
            uses = step.get("uses")
            if uses is not None and str(uses) not in STEP_ACTION_ALLOWLIST:
                errors.append(f"{location}.uses: unapproved action reference {uses!r}")
            run = str(step.get("run", ""))
            step_name = str(step.get("name", ""))
            if _contains_publish_command(run) or PUBLISH_NAME.search(step_name):
                errors.append(f"{location}: deployment/publish step is forbidden")


def _references(data: dict[str, Any]) -> tuple[set[str], set[str]]:
    step_actions: set[str] = set()
    reusable_workflows: set[str] = set()
    jobs = data.get("jobs", {})
    if not isinstance(jobs, dict):
        return step_actions, reusable_workflows
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if "uses" in job:
            reusable_workflows.add(str(job["uses"]))
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and "uses" in step:
                step_actions.add(str(step["uses"]))
    return step_actions, reusable_workflows


def _validate_runners(data: dict[str, Any], name: str, errors: list[str]) -> None:
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job_name, job in jobs.items():
        if not isinstance(job, dict) or "runs-on" not in job:
            continue
        runner = job["runs-on"]
        location = f"{name}.jobs.{job_name}.runs-on"
        if isinstance(runner, str) and runner in STANDARD_RUNNERS:
            continue
        dynamic_policy = DYNAMIC_RUNNER_ALLOWLIST.get(name)
        if dynamic_policy is not None and isinstance(runner, str):
            strategy = job.get("strategy")
            matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
            includes = matrix.get("include") if isinstance(matrix, dict) else None
            if (
                runner == dynamic_policy["expression"]
                and set(matrix or {}) == {"include"}
                and isinstance(includes, list)
                and all(
                    isinstance(item, dict) and isinstance(item.get("runner"), str)
                    for item in includes
                )
                and tuple(item["runner"] for item in includes)
                == dynamic_policy["runners"]
            ):
                continue
        errors.append(f"{location}: nonstandard runner {runner!r} is forbidden")


def validate(root: Path) -> list[str]:
    workflows = root / ".github" / "workflows"
    errors: list[str] = []

    for directory in (root / ".github", workflows):
        if directory.is_symlink():
            errors.append(f"workflow directory path contains symlink: {directory}")
    if errors:
        return errors

    try:
        candidate_root = root.resolve(strict=True)
        workflow_root = workflows.resolve(strict=True)
        workflow_root.relative_to(candidate_root)
    except (OSError, ValueError) as exc:
        errors.append(f"workflow directory must resolve inside candidate root: {exc}")
        return errors

    try:
        actual_paths = list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml"))
    except OSError as exc:
        errors.append(f"unable to discover candidate workflows: {exc}")
        return errors

    safe_paths: dict[str, Path] = {}
    for path in actual_paths:
        if path.is_symlink():
            errors.append(f"{path.name}: symlinked workflow files are forbidden")
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(workflow_root)
        except (OSError, ValueError) as exc:
            errors.append(
                f"{path.name}: workflow must resolve inside candidate workflow directory: {exc}"
            )
            continue
        if not resolved.is_file():
            errors.append(f"{path.name}: workflow entry must be a regular file")
            continue
        safe_paths[path.name] = resolved

    actual = {path.name for path in actual_paths}
    expected = set(WORKFLOW_TRIGGERS)

    reappeared = sorted(actual & FORBIDDEN_WORKFLOWS)
    if reappeared:
        errors.append(f"forbidden upstream workflows reappeared: {', '.join(reappeared)}")

    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        errors.append(f"required fork workflows missing: {', '.join(missing)}")
    if unexpected:
        errors.append(f"unexpected workflows are not allowed: {', '.join(unexpected)}")

    seen_step_actions: set[str] = set()
    seen_reusable_workflows: set[str] = set()
    for name in sorted(safe_paths.keys() & expected):
        path = safe_paths[name]
        try:
            data = _load(path)
            actual_triggers = _triggers(data, path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        expected_triggers = WORKFLOW_TRIGGERS[name]
        if actual_triggers != expected_triggers:
            errors.append(
                f"{name}: triggers {sorted(actual_triggers)} != allowed "
                f"{sorted(expected_triggers)}"
            )
        expected_trigger_config = WORKFLOW_TRIGGER_CONFIGS.get(name)
        if expected_trigger_config is not None and data.get("on") != expected_trigger_config:
            errors.append(f"{name}: trigger configuration differs from the allowed policy")

        expected_permissions = WORKFLOW_PERMISSIONS[name]
        actual_permissions = data.get("permissions")
        if actual_permissions is None:
            errors.append(f"{name}: explicit top-level permissions are required")
        elif actual_permissions != expected_permissions:
            errors.append(
                f"{name}.permissions: {actual_permissions!r} != allowed "
                f"{expected_permissions!r}"
            )
        _validate_security(data, name, errors, expected_permissions)
        _validate_steps(data, name, errors)
        _validate_runners(data, name, errors)
        step_actions, reusable_workflows = _references(data)
        seen_step_actions.update(step_actions)
        seen_reusable_workflows.update(reusable_workflows)
        if name == "fork-policy.yml" and data not in (
            FORK_POLICY_WORKFLOW,
            FORK_POLICY_WORKFLOW_NEXT,
        ):
            errors.append("fork-policy.yml: trusted workflow structure differs from exact policy")

    stable_step_actions = set(STEP_ACTION_ALLOWLIST) - set(SETUP_UV_ACTION_FAMILY)
    missing_step_actions = stable_step_actions - seen_step_actions
    unexpected_step_actions = seen_step_actions - set(STEP_ACTION_ALLOWLIST)
    setup_uv_actions = seen_step_actions & set(SETUP_UV_ACTION_FAMILY)
    if missing_step_actions or unexpected_step_actions or len(setup_uv_actions) != 1:
        errors.append(
            "step action references differ from exact allowlist: "
            f"missing={sorted(missing_step_actions)}, "
            f"extra={sorted(unexpected_step_actions)}, "
            f"setup_uv={sorted(setup_uv_actions)}"
        )
    if seen_reusable_workflows != set(JOB_REUSABLE_WORKFLOW_ALLOWLIST):
        errors.append(
            "job reusable workflow references differ from exact allowlist: "
            f"missing={sorted(set(JOB_REUSABLE_WORKFLOW_ALLOWLIST) - seen_reusable_workflows)}, "
            f"extra={sorted(seen_reusable_workflows - set(JOB_REUSABLE_WORKFLOW_ALLOWLIST))}"
        )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    errors = validate(args.root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"workflow policy OK: {len(WORKFLOW_TRIGGERS)} exact workflows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
