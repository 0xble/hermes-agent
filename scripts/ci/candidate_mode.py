#!/usr/bin/env python3
"""Select the exact-head gate from immutable ancestry, never a missing executable."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The fork's first portable-gate commit. Deleting bin/ci cannot erase ancestry.
FORK_GATE_ROOT = "d77a654d8605200b81aea26ab801dba3ea07cf7a"


def git(root: Path, *args: str, index: Path | None = None) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, encoding="utf-8",
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
             "GIT_TERMINAL_PROMPT": "0", **({"GIT_INDEX_FILE": str(index)} if index else {})},
    ).strip()


OVERLAY_PATHS = frozenset({
    ".github/workflows/gate.yml",
    ".github/workflows/prompt-surface-diff.yml",
    "scripts/ci/candidate_mode.py",
})


def prepare_staging(root: Path, head: str, upstream: str, policy: str) -> str:
    """Create an overlay commit without changing any checkout, index or branch."""
    base = git(root, "merge-base", upstream, head)
    anchor = git(root, "rev-parse", f"{base}^1") if head == base else base
    with tempfile.TemporaryDirectory(prefix="candidate-overlay-") as temporary:
        index = Path(temporary) / "index"
        git(root, "read-tree", anchor, index=index)
        workflows = git(root, "ls-tree", "-r", "--name-only", anchor,
                        ".github/workflows").splitlines()
        for path in workflows:
            if path not in OVERLAY_PATHS:
                git(root, "update-index", "--force-remove", path, index=index)
        for path in sorted(OVERLAY_PATHS):
            entry = git(root, "ls-tree", policy, path).split()
            if len(entry) != 4 or entry[0] not in {"100644", "100755"} or entry[1] != "blob":
                raise ValueError(f"Policy requires a regular overlay file: {path}")
            git(root, "update-index", "--add", "--cacheinfo", entry[0], entry[2], path, index=index)
        tree = git(root, "write-tree", index=index)
    staging = git(root, "-c", "user.name=Fork qualification", "-c",
                  "user.email=qualification@example.invalid", "commit-tree", tree, "-p", anchor, "-m",
                  "ci: temporary policy-only qualification base\n\nFork-Patch: evidence")
    check_staging(root, head, base, staging)
    # Check before publishing: GitHub can activate workflows as soon as a PR
    # opens. Candidate workflow edits must not re-enable a paid upstream lane.
    merged = git(root, "merge-tree", "--write-tree", staging, head)
    merged_workflows = git(root, "ls-tree", "-r", merged, ".github/workflows")
    if merged_workflows != git(root, "ls-tree", "-r", staging, ".github/workflows"):
        raise ValueError("Candidate merge changes policy workflows; do not publish this probe")
    return staging


def check_staging(root: Path, head: str, base: str, staging: str) -> None:
    """Reject candidate code hidden in a qualification-only policy base.

    A baseline head already belongs to upstream: put its overlay on the first
    parent instead so GitHub has a head-only commit and can create a PR.
    The lint comparison base remains the exact baseline, not that parent.
    """
    anchor = git(root, "rev-parse", f"{base}^1") if head == base else base
    if git(root, "merge-base", "--all", head, staging) != anchor:
        raise ValueError("Contaminated staging base: unexpected candidate/staging merge-base")
    if git(root, "merge-base", "--all", anchor, staging) != anchor:
        raise ValueError("Staging base does not descend from the expected overlay anchor")
    commits = git(root, "rev-list", "--reverse", f"{anchor}..{staging}").splitlines()
    if not commits:
        raise ValueError("Staging base is missing the policy overlay")
    for commit in commits:
        if len(git(root, "rev-list", "--parents", "-n", "1", commit).split()) != 2:
            raise ValueError("Contaminated staging base: overlay merge commits are forbidden")
        changes = git(root, "diff-tree", "--no-commit-id", "--name-status", "-z",
                      "--no-renames", "-r", f"{commit}^1", commit).rstrip("\0").split("\0")
        for status, path in zip(changes[::2], changes[1::2], strict=True):
            workflow_deletion = (status == "D" and Path(path).parent.as_posix()
                                 == ".github/workflows" and path.endswith((".yml", ".yaml")))
            if path not in OVERLAY_PATHS and not workflow_deletion:
                raise ValueError(f"Contaminated staging base: {commit} changes {path}")
    workflows = git(root, "ls-tree", "-r", "--name-only", staging, ".github/workflows").splitlines()
    if set(workflows) != {path for path in OVERLAY_PATHS if path.endswith(".yml")}:
        raise ValueError("Staging base must contain only the two free policy workflows")
    print(f"Verified policy-only staging {staging}; overlay anchor: {anchor}; lint base: {base}",
          file=sys.stderr)


def select(root: Path, expected: str, upstream: str, staging: str) -> tuple[str, str]:
    for sha in (expected, upstream, staging):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("A full frozen SHA is required")
    if git(root, "rev-parse", "HEAD") != expected:
        raise ValueError("Candidate checkout is not the exact PR head")
    # Missing history is an error, not permission to take the upstream path.
    git(root, "cat-file", "-e", f"{FORK_GATE_ROOT}^{{commit}}")
    ancestors = git(root, "merge-base", FORK_GATE_ROOT, expected)
    if ancestors == FORK_GATE_ROOT:
        if not git(root, "ls-tree", expected, "bin/ci"):
            raise ValueError("Fork candidate deleted bin/ci; refusing downgrade")
        return "fork", ""
    base = git(root, "merge-base", upstream, expected)
    if git(root, "ls-tree", expected, "bin/ci"):
        raise ValueError("Unknown gate lineage; refusing upstream mode")
    for path in ("scripts/check", "scripts/run_tests.sh", ".github/workflows/lint.yml",
                 ".github/workflows/tests.yml", ".github/workflows/js-tests.yml",
                 ".github/actions/setup-pm/action.yml"):
        if not git(root, "ls-tree", expected, path):
            raise ValueError(f"Unsupported upstream candidate: missing {path}")
    check_staging(root, expected, base, staging)
    return "upstream", base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("head")
    parser.add_argument("upstream")
    parser.add_argument("staging", nargs="?")
    parser.add_argument("--prepare-staging", metavar="POLICY_SHA")
    args = parser.parse_args()
    if args.prepare_staging:
        for sha in (args.head, args.upstream, args.prepare_staging):
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                parser.error("A full frozen SHA is required")
        print(prepare_staging(args.candidate, args.head, args.upstream, args.prepare_staging))
        return
    if not args.staging:
        parser.error("the frozen staging base SHA is required")
    print(f"Frozen upstream tip: {args.upstream}; staging base: {args.staging}")
    mode, base = select(args.candidate, args.head, args.upstream, args.staging)
    print(f"Exact candidate {args.head}: {mode}; upstream comparison base: {base}")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"mode={mode}\nupstream_base={base}\n")


if __name__ == "__main__":
    main()
