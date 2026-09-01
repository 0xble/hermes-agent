from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ci.validate_workflow_policy import (
    FORK_POLICY_WORKFLOW,
    FORK_POLICY_WORKFLOWS,
    JOB_REUSABLE_WORKFLOW_ALLOWLIST,
    STEP_ACTION_ALLOWLIST,
    TRANSITION_ACTION_FAMILIES,
    WORKFLOW_PERMISSIONS,
    WORKFLOW_TRIGGERS,
    _load,
    _references,
    _triggers,
    validate,
)


def _copy_workflows(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / "repo"
    shutil.copytree(root / ".github" / "workflows", target / ".github" / "workflows")
    return target


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_repository_workflow_policy_passes() -> None:
    root = Path(__file__).resolve().parents[1]
    assert validate(root) == []


def test_setup_uv_pin_can_transition_atomically(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    old = "astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39"
    new = "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d"
    for workflow in (root / ".github" / "workflows").glob("*.y*"):
        text = workflow.read_text(encoding="utf-8")
        workflow.write_text(text.replace(old, new), encoding="utf-8")
    assert validate(root) == []


def test_setup_uv_pin_cannot_be_mixed_during_transition(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    workflow = root / ".github" / "workflows" / "e2e-desktop.yml"
    text = workflow.read_text(encoding="utf-8")
    old = "astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39"
    new = "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d"
    source, target = (old, new) if old in text else (new, old)
    _replace(workflow, source, target)
    errors = validate(root)
    assert any("step action references differ from exact allowlist" in error for error in errors)


def test_checkout_pin_can_transition_with_explicit_fork_data_opt_in(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    old = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    new = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    for workflow in (root / ".github" / "workflows").glob("*.y*"):
        text = workflow.read_text(encoding="utf-8")
        workflow.write_text(text.replace(old, new), encoding="utf-8")
    fork_policy = root / ".github" / "workflows" / "fork-policy.yml"
    _replace(
        fork_policy,
        "          fetch-depth: 0\n          persist-credentials: false\n",
        "          fetch-depth: 0\n          persist-credentials: false\n"
        "          allow-unsafe-pr-checkout: true\n",
    )
    assert validate(root) == []


def test_grouped_action_pins_can_transition_atomically(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    replacements = {
        "cachix/install-nix-action@630ae543ea3a38a9a4166f03376c02c50f408342":
            "cachix/install-nix-action@13d8dd58da0234aa297dedd986986ccb8e7f3e24",
        "google/osv-scanner-action/osv-scanner-action@9a498708959aeaef5ef730655706c5a1df1edbc2":
            "google/osv-scanner-action/osv-scanner-action@6e4298ebc4db23e847df9b2e2de2939d6f066c67",
        "hadolint/hadolint-action@54c9adbab1582c2ef04b2016b760714a4bfde3cf":
            "hadolint/hadolint-action@06be81baf89a55ffd0e24b8f04a4185738dd3387",
    }
    for workflow in (root / ".github" / "workflows").glob("*.y*"):
        text = workflow.read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        workflow.write_text(text, encoding="utf-8")
    assert validate(root) == []


def test_ci_inherits_osv_actions_read_permission() -> None:
    expected = {"actions": "read", "contents": "read"}
    assert WORKFLOW_PERMISSIONS["ci.yaml"] == expected
    assert WORKFLOW_PERMISSIONS["osv-scanner.yml"] == expected


def test_inline_triggers_are_parsed_as_yaml(tmp_path: Path) -> None:
    path = tmp_path / "inline.yml"
    path.write_text("name: Inline\non: [pull_request, push]\njobs: {}\n", encoding="utf-8")
    assert _triggers(_load(path), path) == frozenset({"pull_request", "push"})


def test_aliased_triggers_are_resolved_as_yaml(tmp_path: Path) -> None:
    path = tmp_path / "aliased.yml"
    path.write_text(
        "x-triggers: &fork-triggers [pull_request, push]\n"
        "name: Aliased\n"
        "on: *fork-triggers\n"
        "jobs: {}\n",
        encoding="utf-8",
    )
    assert _triggers(_load(path), path) == frozenset({"pull_request", "push"})


def test_forbidden_workflow_path_reappearance_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    (root / ".github" / "workflows" / "docker.yml").write_text(
        "name: Docker\non: [pull_request]\njobs: {}\n", encoding="utf-8"
    )
    errors = validate(root)
    assert any("forbidden upstream workflows reappeared: docker.yml" in error for error in errors)


def test_symlinked_workflow_file_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    trusted = tmp_path / "trusted-policy" / ".github" / "workflows"
    trusted.mkdir(parents=True)
    source = root / ".github" / "workflows" / "fork-policy.yml"
    trusted_policy = trusted / source.name
    shutil.copy2(source, trusted_policy)
    source.unlink()
    source.symlink_to(trusted_policy)

    errors = validate(root)

    assert any("fork-policy.yml" in error and "symlink" in error for error in errors)


@pytest.mark.parametrize("symlinked_directory", [".github", ".github/workflows"])
def test_symlinked_workflow_directory_escape_fails_closed(
    tmp_path: Path, symlinked_directory: str
) -> None:
    root = _copy_workflows(tmp_path)
    escaped = tmp_path / "escaped"
    shutil.copytree(root / ".github" / "workflows", escaped / ".github" / "workflows")
    path = root / symlinked_directory
    if path.is_dir():
        shutil.rmtree(path)
    path.symlink_to(escaped / symlinked_directory, target_is_directory=True)

    errors = validate(root)

    assert any("workflow directory" in error and "symlink" in error for error in errors)


def test_unexpected_schedule_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    ci = root / ".github" / "workflows" / "ci.yaml"
    _replace(
        ci,
        "on:\n  pull_request:\n  push:\n    branches: [main]\n",
        "on: [pull_request, push, schedule]\n",
    )
    errors = validate(root)
    assert any("ci.yaml: triggers" in error and "schedule" in error for error in errors)


def test_write_permission_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    ci = root / ".github" / "workflows" / "ci.yaml"
    _replace(ci, "  actions: read\n", "  actions: write\n")
    errors = validate(root)
    assert any("permission 'write' is forbidden" in error for error in errors)


def test_missing_top_level_permissions_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(lint, "permissions:\n  contents: read\n\n", "")
    errors = validate(root)
    assert any("lint.yml: explicit top-level permissions are required" in error for error in errors)


def test_unapproved_read_permission_scope_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(
        lint,
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    permissions:\n      issues: read\n",
    )
    errors = validate(root)
    assert any("lint.yml.jobs" in error and "issues" in error for error in errors)


def test_secret_and_environment_fail_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(
        lint,
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    environment: production\n    secrets: inherit\n",
    )
    errors = validate(root)
    assert any("secrets and deployment environments are forbidden" in error for error in errors)


def test_bracket_style_secret_expression_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(
        lint,
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    env:\n      TOKEN: ${{ secrets['TOKEN'] }}\n",
    )
    errors = validate(root)
    assert any("secret references are forbidden" in error for error in errors)


def test_broadened_push_branches_fail_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    ci = root / ".github" / "workflows" / "ci.yaml"
    _replace(ci, "    branches: [main]\n", "    branches: [main, develop]\n")
    errors = validate(root)
    assert any("ci.yaml: trigger configuration" in error for error in errors)


def test_narrowed_pull_request_branches_fail_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    ci = root / ".github" / "workflows" / "ci.yaml"
    _replace(ci, "  pull_request:\n", "  pull_request:\n    branches: [main]\n")
    errors = validate(root)
    assert any("ci.yaml: trigger configuration" in error for error in errors)


def test_changed_or_additional_osv_schedule_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    osv = root / ".github" / "workflows" / "osv-scanner.yml"
    _replace(
        osv,
        "    - cron: '0 9 * * 1'\n",
        "    - cron: '0 9 * * 2'\n    - cron: '0 9 * * 3'\n",
    )
    errors = validate(root)
    assert any("osv-scanner.yml: trigger configuration" in error for error in errors)


def test_deployment_or_publish_step_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(
        lint,
        "    steps:\n",
        "    steps:\n      - name: Publish package\n        run: npm publish\n",
    )
    errors = validate(root)
    assert any("deployment/publish step is forbidden" in error for error in errors)


def test_cargo_publish_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(
        lint,
        "    steps:\n",
        "    steps:\n      - name: Release Rust crate\n        run: cargo publish\n",
    )
    errors = validate(root)
    assert any("deployment/publish step is forbidden" in error for error in errors)


@pytest.mark.parametrize(
    "command",
    [
        "cargo +stable publish",
        "cargo --locked publish",
        "command cargo +nightly --config net.git-fetch-with-cli=true publish",
        "env CARGO_TERM_COLOR=never /usr/bin/cargo +stable --locked publish",
        "cargo \\\n          +stable \\\n          --locked \\\n          publish",
    ],
)
def test_interposed_cargo_publish_fails_closed(tmp_path: Path, command: str) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(
        lint,
        "    steps:\n",
        f"    steps:\n      - name: Release Rust crate\n        run: {command}\n",
    )
    errors = validate(root)
    assert any("deployment/publish step is forbidden" in error for error in errors)


@pytest.mark.parametrize(
    "action",
    [
        "JS-DevTools/npm-publish@v3",
        "cloudflare/wrangler-action@v3",
        "./.github/actions/deploy",
    ],
)
def test_unapproved_step_action_reference_fails_closed(
    tmp_path: Path, action: str
) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(
        lint,
        "    steps:\n",
        f"    steps:\n      - name: Invoke action\n        uses: {action}\n",
    )
    errors = validate(root)
    assert any("unapproved action reference" in error and action in error for error in errors)


def test_external_job_reusable_workflow_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    ci = root / ".github" / "workflows" / "ci.yaml"
    _replace(
        ci,
        "    uses: ./.github/workflows/tests.yml\n",
        "    uses: owner/repo/.github/workflows/tests.yml@main\n",
    )
    errors = validate(root)
    assert any("unapproved reusable workflow reference" in error for error in errors)


def test_unapproved_local_job_reusable_workflow_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    ci = root / ".github" / "workflows" / "ci.yaml"
    _replace(
        ci,
        "    uses: ./.github/workflows/tests.yml\n",
        "    uses: ./.github/workflows/deploy-site.yml\n",
    )
    errors = validate(root)
    assert any("unapproved reusable workflow reference" in error for error in errors)


def test_nonstandard_runner_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(lint, "runs-on: ubuntu-latest", "runs-on: ubuntu-latest-32-core")
    errors = validate(root)
    assert any("nonstandard runner" in error for error in errors)


def test_arbitrary_dynamic_runner_expression_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    workflow = root / ".github" / "workflows" / "tests-os.yml"
    _replace(
        workflow,
        "runs-on: ${{ matrix.runner }}",
        "runs-on: ${{ github.event.inputs.runner }}",
    )
    errors = validate(root)
    assert any("nonstandard runner" in error for error in errors)


def test_dynamic_matrix_cannot_add_self_hosted_runner(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    workflow = root / ".github" / "workflows" / "tests-os.yml"
    _replace(
        workflow,
        "      matrix:\n        include:\n",
        "      matrix:\n        runner: [self-hosted]\n        include:\n",
    )
    errors = validate(root)
    assert any("nonstandard runner" in error for error in errors)


def test_fork_policy_workflow_has_exact_trusted_structure() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / ".github" / "workflows" / "fork-policy.yml"
    assert _load(path) in FORK_POLICY_WORKFLOWS


def test_fork_policy_run_commands_resolve_only_trusted_scripts(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted-policy" / "scripts"
    candidate = tmp_path / "candidate"
    bin_dir = tmp_path / "bin"
    (trusted / "ci").mkdir(parents=True)
    (candidate / "scripts" / "ci").mkdir(parents=True)
    bin_dir.mkdir()
    (candidate / "MAINTENANCE.md").write_text("candidate data\n", encoding="utf-8")

    marker = tmp_path / "trusted-runs"
    maintenance_script = trusted / "validate_maintenance_manifest.py"
    maintenance_script.write_text(
        "from pathlib import Path\n"
        "import os, sys\n"
        "assert sys.argv[1] == 'candidate/MAINTENANCE.md'\n"
        "assert Path(sys.argv[1]).read_text() == 'candidate data\\n'\n"
        "Path(os.environ['TRUSTED_MARKER']).write_text('maintenance\\n')\n",
        encoding="utf-8",
    )
    policy_script = trusted / "ci" / "validate_workflow_policy.py"
    policy_script.write_text(
        "from pathlib import Path\n"
        "import os, sys\n"
        "assert sys.argv[1:] == ['--root', 'candidate']\n"
        "with Path(os.environ['TRUSTED_MARKER']).open('a') as handle:\n"
        "    handle.write('policy\\n')\n",
        encoding="utf-8",
    )
    for candidate_script in (
        candidate / "scripts" / "validate_maintenance_manifest.py",
        candidate / "scripts" / "ci" / "validate_workflow_policy.py",
    ):
        candidate_script.write_text("raise SystemExit(99)\n", encoding="utf-8")

    git = bin_dir / "git"
    git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    git.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "test \"$1\" = run && test \"$2\" = --script || exit 98\n"
        "shift 2\n"
        "exec python3 \"$@\"\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)

    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TRUSTED_MARKER": str(marker),
    }
    steps = FORK_POLICY_WORKFLOW["jobs"]["policy"]["steps"]
    for step in steps:
        if "run" in step:
            subprocess.run(
                ["/bin/bash", "-euo", "pipefail", "-c", step["run"]],
                cwd=tmp_path,
                env=env,
                check=True,
            )

    assert marker.read_text(encoding="utf-8") == "maintenance\npolicy\n"


def test_modified_candidate_policy_workflow_fails_closed(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    policy = root / ".github" / "workflows" / "fork-policy.yml"
    _replace(
        policy,
        "trusted-policy/scripts/ci/validate_workflow_policy.py",
        "candidate/scripts/ci/validate_workflow_policy.py",
    )
    errors = validate(root)
    assert any("trusted workflow structure differs from exact policy" in error for error in errors)


def test_candidate_noop_validator_cannot_self_validate(tmp_path: Path) -> None:
    root = _copy_workflows(tmp_path)
    candidate_validator = root / "scripts" / "ci" / "validate_workflow_policy.py"
    candidate_validator.parent.mkdir(parents=True)
    candidate_validator.write_text(
        "def validate(_root):\n    return []\n",
        encoding="utf-8",
    )
    lint = root / ".github" / "workflows" / "lint.yml"
    _replace(
        lint,
        "    steps:\n",
        "    steps:\n      - name: Invoke action\n"
        "        uses: JS-DevTools/npm-publish@v3\n",
    )

    # This imported validator represents trusted default-branch policy code;
    # the candidate's replacement is data and is never imported or executed.
    errors = validate(root)
    assert any("unapproved action reference" in error for error in errors)


def test_removed_workflow_comments_do_not_reappear() -> None:
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    retained_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(list(root.glob("*.yml")) + list(root.glob("*.yaml")))
    )
    assert "deploy-site.yml" not in retained_text
    assert "publish-e2e-evidence.yml" not in retained_text
    assert "workflow_run publisher" not in retained_text


def test_inventory_is_exact() -> None:
    assert len(WORKFLOW_TRIGGERS) == 19


def test_action_and_reusable_reference_allowlists_are_exact() -> None:
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    step_actions: set[str] = set()
    reusable_workflows: set[str] = set()
    for path in sorted(list(root.glob("*.yml")) + list(root.glob("*.yaml"))):
        actions, reusable = _references(_load(path))
        step_actions.update(actions)
        reusable_workflows.update(reusable)

    transition_actions = set().union(*TRANSITION_ACTION_FAMILIES)
    stable_allowlist = set(STEP_ACTION_ALLOWLIST) - transition_actions
    assert step_actions - transition_actions == stable_allowlist
    for family in TRANSITION_ACTION_FAMILIES:
        assert len(step_actions & set(family)) == 1
    assert reusable_workflows == set(JOB_REUSABLE_WORKFLOW_ALLOWLIST)
