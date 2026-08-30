import json
import re
import shutil
import subprocess
from pathlib import Path


BASH = next(
    (
        str(path)
        for path in (Path("/opt/homebrew/bin/bash"), Path("/usr/local/bin/bash"))
        if path.exists()
    ),
    shutil.which("bash") or "bash",
)
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "install-e2e.yml"
PICKER = REPO_ROOT / "scripts" / "sandbox" / "pick-release-tags.sh"
CANONICAL_URL = "https://github.com/NousResearch/hermes-agent.git"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )


def init_repo(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "install-e2e@example.com")
    git(path, "config", "user.name", "Install E2E")


def commit(repo: Path, value: str) -> None:
    (repo / "release.txt").write_text(f"{value}\n")
    git(repo, "add", "release.txt")
    git(repo, "commit", "-q", "-m", value)


def clone_bare(source: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(destination)],
        check=True,
    )


def clone_without_tags(source: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", "-q", "--no-tags", str(source), str(destination)],
        check=True,
    )


def workflow_fetch_script(workflow: str) -> str:
    match = re.search(
        r"(?m)^      - name: Fetch canonical release tags\n"
        r"        run: \|\n(?P<body>(?:          [^\n]*\n)+)",
        workflow,
    )
    assert match, "install E2E must explicitly fetch canonical release tags"
    return "\n".join(line[10:] for line in match.group("body").splitlines())


def test_install_e2e_sources_only_canonical_release_tags(tmp_path: Path) -> None:
    canonical_work = tmp_path / "canonical-work"
    init_repo(canonical_work)
    releases = [
        "v2026.1.1",
        "v2026.2.2",
        "v2026.3.3",
        "v2026.4.4",
        "v2026.5.5",
    ]
    for release in releases:
        commit(canonical_work, release)
        git(canonical_work, "tag", "-a", release, "-m", release)
    git(canonical_work, "tag", "-a", "release-candidate", "-m", "not official")
    canonical = tmp_path / "canonical.git"
    clone_bare(canonical_work, canonical)

    origin_work = tmp_path / "origin-work"
    init_repo(origin_work)
    commit(origin_work, "fork main")
    git(origin_work, "tag", "-a", "archive/pre-rebase", "-m", "archive only")
    origin = tmp_path / "origin.git"
    clone_bare(origin_work, origin)

    # Reproduce the old checkout/fetch behavior: fetching every origin tag gives
    # this standalone mirror only archive tags, so the local picker has no input.
    old_checkout = tmp_path / "old-checkout"
    clone_without_tags(origin, old_checkout)
    git(old_checkout, "fetch", "-q", "--force", "--tags", "origin")
    old_pick = subprocess.run(
        [BASH, str(PICKER), "--count", "3", "--repo", str(old_checkout)],
        text=True,
        capture_output=True,
    )
    assert old_pick.returncode != 0
    assert "no release tags found" in old_pick.stderr

    checkout = tmp_path / "checkout"
    clone_without_tags(origin, checkout)
    git(checkout, "config", "user.email", "install-e2e@example.com")
    git(checkout, "config", "user.name", "Install E2E")
    git(checkout, "tag", "-a", "v2026.3.3", "-m", "stale local conflict")
    stale_target = git(checkout, "rev-parse", "v2026.3.3^{}").stdout.strip()

    workflow = WORKFLOW.read_text()
    fetch_script = workflow_fetch_script(workflow)
    assert CANONICAL_URL in fetch_script
    assert "+refs/tags/v*:refs/tags/v*" in fetch_script
    assert "--force" in fetch_script
    assert "--no-tags" in fetch_script
    assert "--filter=blob:none" in fetch_script
    subprocess.run(
        [
            BASH,
            "-euo",
            "pipefail",
            "-c",
            fetch_script.replace(CANONICAL_URL, canonical.as_uri()),
        ],
        cwd=checkout,
        check=True,
        text=True,
        capture_output=True,
    )

    local_tags = git(checkout, "tag", "--list").stdout.splitlines()
    assert local_tags == releases
    assert (
        git(checkout, "cat-file", "-t", "refs/tags/v2026.3.3").stdout.strip() == "tag"
    )
    canonical_target = git(canonical, "rev-parse", "v2026.3.3^{}").stdout.strip()
    assert canonical_target != stale_target
    assert git(checkout, "rev-parse", "v2026.3.3^{}").stdout.strip() == canonical_target

    picked = subprocess.run(
        [BASH, str(PICKER), "--count", "3", "--repo", str(checkout)],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(picked.stdout) == ["v2026.1.1", "v2026.3.3", "v2026.5.5"]

    # Fetching into the ephemeral checkout must not mirror canonical tags back
    # into the standalone origin.
    assert git(origin, "tag", "--list", "v*").stdout == ""

    trigger_block = workflow.split("permissions:", 1)[0]
    assert "  workflow_dispatch:" in trigger_block
    assert "  schedule:" in trigger_block
    assert "  push:" not in trigger_block
    assert "if: github.ref == 'refs/heads/main'" in workflow


def test_install_e2e_manual_tag_count_is_shell_safe() -> None:
    workflow = WORKFLOW.read_text()

    checkout = re.search(
        r"(?m)^      - uses: actions/checkout@[^\n]+\n"
        r"        with:\n(?P<body>(?:          [^\n]+\n)+)",
        workflow,
    )
    assert checkout, "install E2E must configure its checkout explicitly"
    assert "persist-credentials: false" in checkout.group("body")

    picker = re.search(
        r"(?m)^      - id: pick\n"
        r"        env:\n"
        r"          TAG_COUNT: \$\{\{ inputs\.tag-count \|\| 5 \}\}\n"
        r"        run: \|\n(?P<body>(?:          [^\n]*\n)+)",
        workflow,
    )
    assert picker, "manual tag count must enter the picker through TAG_COUNT"
    run_block = "\n".join(
        line[10:] for line in picker.group("body").splitlines()
    )
    assert '--count "$TAG_COUNT"' in run_block
    assert "${{ inputs" not in run_block
