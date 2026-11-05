#!/usr/bin/env python3
"""Select the exact-head gate from immutable ancestry, never a missing executable."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

# The fork's first portable-gate commit. Deleting bin/ci cannot erase ancestry.
FORK_GATE_ROOT = "d77a654d8605200b81aea26ab801dba3ea07cf7a"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, encoding="utf-8",
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
             "GIT_TERMINAL_PROMPT": "0"},
    ).strip()


def select(root: Path, expected: str, upstream: str) -> tuple[str, str]:
    for sha in (expected, upstream):
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
    return "upstream", base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("head")
    parser.add_argument("upstream")
    args = parser.parse_args()
    mode, base = select(args.candidate, args.head, args.upstream)
    print(f"Exact candidate {args.head}: {mode}; upstream comparison base: {base}")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"mode={mode}\nupstream_base={base}\n")


if __name__ == "__main__":
    main()
