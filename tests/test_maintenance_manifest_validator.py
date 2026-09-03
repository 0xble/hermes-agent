from pathlib import Path
import subprocess

import pytest

from scripts.validate_maintenance_manifest import _validate_registration_history, validate_manifest


INDEX = """## Maintained patch index

| ID | Status | Stable commit subject | Purpose |
| --- | --- | --- | --- |
| HERMES-001 | Active | `fix: one` | First patch. |
| HERMES-002 | Retired | `fix: two`; `docs: retire two` | Second patch. |

## Patch records
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "MAINTENANCE.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_manifest_has_one_index_row_and_record_per_id(tmp_path):
    path = _write(
        tmp_path,
        INDEX
        + """
### HERMES-001 — One

- **Upstream tracking:** None.
- **Upstream PR:** None after checked 2026-08-18.

### HERMES-002 — Two

- **Upstream tracking:** Released replacement.
- **Upstream PR:** None after checked 2026-08-18.
""",
    )

    assert validate_manifest(path) == []


def test_duplicate_record_id_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        INDEX
        + """
### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-001 — Duplicate
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-002 — Two
- **Upstream tracking:** None.
- **Upstream PR:** None.
""",
    )

    errors = validate_manifest(path)

    assert "duplicate patch record ID: HERMES-001" in errors


def test_unindexed_record_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        INDEX
        + """
### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-002 — Two
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-003 — Missing row
- **Upstream tracking:** None.
- **Upstream PR:** None.
""",
    )

    assert "unindexed patch record: HERMES-003" in validate_manifest(path)


def test_index_row_without_record_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        INDEX
        + """
### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
""",
    )

    assert "indexed patch has no record: HERMES-002" in validate_manifest(path)


def test_duplicate_index_row_is_rejected(tmp_path):
    duplicate_index = INDEX.replace(
        "\n## Patch records",
        "\n| HERMES-001 | Active | `fix: duplicate` | Duplicate. |\n\n## Patch records",
    )
    path = _write(
        tmp_path,
        duplicate_index
        + """
### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-002 — Two
- **Upstream tracking:** None.
- **Upstream PR:** None.
""",
    )

    assert "duplicate patch index ID: HERMES-001" in validate_manifest(path)


def test_record_requires_upstream_fields(tmp_path):
    path = _write(
        tmp_path,
        INDEX
        + """
### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-002 — Two
- **Upstream tracking:** Released replacement.
""",
    )

    assert "HERMES-002 record missing Upstream PR field" in validate_manifest(path)


def test_unindexed_fork_subject_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        INDEX
        + """
### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-002 — Two
- **Upstream tracking:** None.
- **Upstream PR:** None.
""",
    )

    errors = validate_manifest(
        path,
        fork_subjects={"fix: one", "fix: two", "docs: retire two", "fix: orphan"},
    )

    assert "fork-only subject is neither indexed nor exempt: fix: orphan" in errors


def test_duplicate_post_baseline_commit_subject_is_rejected(tmp_path):
    repo, _manifest, baseline = _init_history_repo(tmp_path)
    (repo / "duplicate.py").write_text("duplicate = True\n", encoding="utf-8")
    _git(repo, "add", "duplicate.py")
    _git(repo, "commit", "-m", "fix: one")

    errors = _validate_registration_history(repo, baseline)

    assert any(
        "fork commit reuses an earlier subject" in error and "fix: one" in error
        for error in errors
    )


def test_explicit_administrative_subject_exemption_is_accepted(tmp_path):
    path = _write(
        tmp_path,
        INDEX
        + """
## Fork-only administrative subject exemptions

| Stable commit subject | Narrow non-patch reason |
| --- | --- |
| `chore: regenerate formatter output` | Mechanical generated output only. |

### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-002 — Two
- **Upstream tracking:** None.
- **Upstream PR:** None.
""",
    )

    assert validate_manifest(
        path,
        fork_subjects={
            "fix: one",
            "fix: two",
            "docs: retire two",
            "chore: regenerate formatter output",
        },
    ) == []


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _init_history_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    manifest = _write(
        repo,
        INDEX
        + """
### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-002 — Two
- **Upstream tracking:** None.
- **Upstream PR:** None.
""",
    )
    _git(repo, "add", "MAINTENANCE.md")
    _git(repo, "commit", "-m", "fix: one")
    return repo, manifest, _git(repo, "rev-parse", "HEAD")


def test_history_validation_rejects_post_hoc_patch_registration(tmp_path):
    repo, manifest, baseline = _init_history_repo(tmp_path)
    (repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "fix: orphan")

    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "`fix: one`", "`fix: one`; `fix: orphan`"
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "MAINTENANCE.md")
    _git(repo, "commit", "-m", "docs: register orphan")

    errors = validate_manifest(manifest, history_baseline=baseline)

    assert any(
        "fork subject was not registered in its own commit" in error
        and "fix: orphan" in error
        for error in errors
    )


def test_history_validation_accepts_same_commit_registration(tmp_path):
    repo, manifest, baseline = _init_history_repo(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "`fix: one`", "`fix: one`; `fix: registered`"
        ),
        encoding="utf-8",
    )
    (repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "MAINTENANCE.md", "feature.py")
    _git(repo, "commit", "-m", "fix: registered")

    assert validate_manifest(manifest, history_baseline=baseline) == []


def test_trusted_policy_validates_immutable_pull_request_head():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "fork-policy.yml"
    ).read_text(encoding="utf-8")

    assert "pull_request_target:" in workflow
    assert "ref: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "path: trusted-policy" in workflow
    assert "path: candidate" in workflow
    assert "path: canonical-upstream" in workflow
    assert "persist-credentials: false" in workflow
    assert "python3 trusted-policy/scripts/validate_maintenance_manifest.py" in workflow
    assert "trusted-policy/scripts/ci/validate_workflow_policy.py" in workflow
    assert "candidate/scripts/" not in workflow
    assert "HEAD:refs/remotes/canonical-upstream/main" in workflow


def test_history_validation_includes_merge_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "upstream: root")
    _git(repo, "branch", "canonical")

    manifest = _write(
        repo,
        INDEX
        + """
### HERMES-001 — One
- **Upstream tracking:** None.
- **Upstream PR:** None.
### HERMES-002 — Two
- **Upstream tracking:** None.
- **Upstream PR:** None.
""",
    )
    _git(repo, "add", "MAINTENANCE.md")
    _git(repo, "commit", "-m", "fix: one")
    baseline = _git(repo, "rev-parse", "HEAD")
    fork_branch = _git(repo, "branch", "--show-current")

    _git(repo, "checkout", "canonical")
    (repo / "upstream.py").write_text("upstream = True\n", encoding="utf-8")
    _git(repo, "add", "upstream.py")
    _git(repo, "commit", "-m", "upstream: change")

    _git(repo, "checkout", fork_branch)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "`fix: one`", "`fix: one`; `fix: registered`"
        ),
        encoding="utf-8",
    )
    (repo / "feature.py").write_text("feature = True\n", encoding="utf-8")
    _git(repo, "add", "MAINTENANCE.md", "feature.py")
    _git(repo, "commit", "-m", "fix: registered")
    _git(repo, "merge", "--no-ff", "canonical", "-m", "Merge canonical upstream")

    errors = _validate_registration_history(
        repo, baseline, upstream_ref="canonical"
    )
    assert not any("upstream: change" in error for error in errors)
    assert any(
        "fork subject was not registered in its own commit" in error
        and "Merge canonical upstream" in error
        for error in errors
    )


def test_subject_only_history_baseline_is_rejected(tmp_path):
    repo, _manifest, _baseline = _init_history_repo(tmp_path)

    assert _validate_registration_history(
        repo,
        None,
        upstream_ref="canonical",
        baseline_subject="fix: one",
    ) == [
        "subject-only maintenance history baselines are unsafe; pass an exact "
        "trusted commit with --history-baseline"
    ]
