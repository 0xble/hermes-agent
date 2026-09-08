from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil

import pytest
import yaml

from scripts.ci.validate_workflow_policy import validate

ROOT = Path(__file__).resolve().parents[1]


def candidate(tmp_path: Path) -> Path:
    target = tmp_path / "candidate"
    shutil.copytree(ROOT / ".github", target / ".github")
    return target


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def load_policy(root: Path) -> dict:
    # BaseLoader constructs only strings and containers, never Python objects.
    return yaml.load(
        (root / ".github/workflows/fork-policy.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def write_policy(root: Path, policy: dict) -> None:
    (root / ".github/workflows/fork-policy.yml").write_text(
        yaml.safe_dump(policy, sort_keys=False), encoding="utf-8"
    )


def test_normal_edits_and_privileged_cosmetics_are_allowed(tmp_path: Path) -> None:
    root = candidate(tmp_path)
    lint = root / ".github/workflows/lint.yml"
    replace(lint, "name: Python static checks", "name: Python quality checks")

    policy = load_policy(root)
    policy["name"] = "Renamed workflow"
    policy["jobs"]["policy"]["name"] = "Renamed job"
    for index, step in enumerate(policy["jobs"]["policy"]["steps"]):
        step["name"] = f"Renamed step {index}"
    write_policy(root, {key: policy[key] for key in reversed(policy)})

    assert validate(root, ROOT) == []


def test_untrusted_external_actions_are_rejected(tmp_path: Path) -> None:
    root = candidate(tmp_path)
    replace(
        root / ".github/workflows/lint.yml",
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "attacker/action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    assert any("approved by the trusted base policy" in error for error in validate(root, ROOT))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("python3 scripts/ci/local_check.py --profile smoke", "true", "mandatory smoke/result correctness"),
        ("sys.exit(1)", "sys.exit(0)", "mandatory smoke/result correctness"),
    ],
)
def test_required_ci_gate_cannot_be_self_attested(tmp_path: Path, old: str, new: str, message: str) -> None:
    root = candidate(tmp_path)
    replace(root / ".github/workflows/ci.yaml", old, new)
    assert any(message in error for error in validate(root, ROOT))


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-run",
        "appended-command",
        "if-security",
        "continue-security",
        "candidate-cwd",
        "bash-env",
        "default-shell",
        "candidate-trusted-ref",
        "candidate-trusted-repository",
        "candidate-local-action",
        "substituted-action",
        "gating-advisory",
        "extra-action-input",
    ],
)
def test_privileged_execution_contract_rejects_bypasses(
    tmp_path: Path, mutation: str
) -> None:
    root = candidate(tmp_path)
    policy = deepcopy(load_policy(root))
    job = policy["jobs"]["policy"]
    steps = job["steps"]
    if mutation == "extra-run":
        steps.append({"run": "candidate/scripts/attack.py"})
    elif mutation == "appended-command":
        steps[4]["run"] += "; candidate/attack"
    elif mutation == "if-security":
        steps[4]["if"] = "${{ github.event.pull_request.head.repo.full_name }}"
    elif mutation == "continue-security":
        steps[4]["continue-on-error"] = "true"
    elif mutation == "candidate-cwd":
        steps[4]["working-directory"] = "candidate"
    elif mutation == "bash-env":
        job["env"] = {"BASH_ENV": "candidate/attack.sh"}
    elif mutation == "default-shell":
        policy["defaults"] = {"run": {"shell": "candidate/attack.sh {0}"}}
    elif mutation == "candidate-trusted-ref":
        steps[0]["with"]["ref"] = "${{ github.event.pull_request.head.sha }}"
    elif mutation == "candidate-trusted-repository":
        steps[0]["with"]["repository"] = "${{ github.event.pull_request.head.repo.full_name }}"
    elif mutation == "candidate-local-action":
        steps[1]["uses"] = "./.github/actions/candidate-code"
    elif mutation == "substituted-action":
        steps[3]["uses"] = "owner/setup@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    elif mutation == "gating-advisory":
        steps[2].pop("continue-on-error")
    else:
        steps[0]["with"]["name"] = "candidate-controlled"
    write_policy(root, policy)

    assert any(
        "privileged execution contract" in error for error in validate(root, ROOT)
    )


@pytest.mark.parametrize(
    "trigger",
    [
        "pull_request_target",
        ["pull_request", "pull_request_target"],
        {"pull_request_target": None},
    ],
)
def test_pull_request_target_is_reserved_in_every_yaml_form(
    tmp_path: Path, trigger
) -> None:
    root = candidate(tmp_path)
    (root / ".github/workflows/unapproved.yml").write_text(
        yaml.safe_dump(
            {
                "name": "Unapproved",
                "on": trigger,
                "permissions": {"contents": "read"},
                "jobs": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert any(
        "pull_request_target is reserved" in error for error in validate(root, ROOT)
    )


def test_github_ancestor_symlink_and_local_traversal_are_rejected(tmp_path: Path) -> None:
    root = candidate(tmp_path)
    real_github = tmp_path / "real-github"
    (root / ".github").rename(real_github)
    (root / ".github").symlink_to(real_github, target_is_directory=True)
    assert any(".github must be a real directory" in error for error in validate(root))

    (root / ".github").unlink()
    real_github.rename(root / ".github")
    escaped = root / ".github/escaped"
    escaped.mkdir()
    (escaped / "action.yml").write_text(
        "name: Escaped\nruns:\n  using: composite\n  steps: []\n", encoding="utf-8"
    )
    replace(
        root / ".github/workflows/lint.yml",
        "    steps:\n",
        "    steps:\n      - uses: ./.github/actions/../escaped\n",
    )
    assert any(
        "must be real and under ./.github/actions/" in error
        for error in validate(root)
    )

def test_write_secrets_publish_and_privileged_runner_are_rejected(tmp_path: Path) -> None:
    root = candidate(tmp_path)
    workflow = root / ".github/workflows/lint.yml"
    replace(workflow, "contents: read", "contents: write")
    replace(workflow, "runs-on: ubuntu-latest", "runs-on: self-hosted")
    replace(
        workflow,
        "    steps:\n",
        "    environment: production\n"
        "    env:\n"
        "      TOKEN: ${{ secrets.TOKEN }}\n"
        "    steps:\n"
        "      - name: Publish package\n"
        "        run: npm publish\n",
    )
    errors = validate(root, ROOT)
    assert any("permission is forbidden" in error for error in errors)
    assert any("GitHub-hosted standard runners" in error for error in errors)
    assert any("secrets and deployment environments" in error for error in errors)
    assert any("secret references" in error for error in errors)
    assert any("deployment or publish" in error for error in errors)
