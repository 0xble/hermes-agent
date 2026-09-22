#!/usr/bin/env python3
"""Reject newly added unbounded PyPI specs, preserving the hosted dep-bounds scope."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

# Preserve the workflow's quoted numeric >= spec check, including extras.
# Exact pins, upper-bounded specs and Git references do not match this form.
UNBOUNDED = re.compile(r'"[a-zA-Z0-9_-]+(?:\[[^\]]*\])?>=[ 0-9.]+"')


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ['git', *arguments], cwd=repo, check=True, capture_output=True,
        text=True, encoding='utf-8', errors='replace',
    ).stdout.strip()


def selected_history(repo: Path, base: str, head: str) -> tuple[str, str]:
    """Require complete ancestry of the selected commits, not unrelated refs."""
    base_sha = git(repo, 'rev-parse', '--verify', '--end-of-options', f'{base}^{{commit}}')
    head_sha = git(repo, 'rev-parse', '--verify', '--end-of-options', f'{head}^{{commit}}')
    shallow = Path(git(repo, 'rev-parse', '--git-path', 'shallow'))
    if not shallow.is_absolute():
        shallow = repo / shallow
    if shallow.exists():
        for boundary in shallow.read_text(encoding='utf-8').splitlines():
            for commit in (base_sha, head_sha):
                result = subprocess.run(
                    ['git', 'merge-base', '--is-ancestor', boundary, commit],
                    cwd=repo, capture_output=True, text=True, encoding='utf-8', errors='replace',
                )
                if result.returncode == 0:
                    raise ValueError(f'Full history is required for selected commits: reachable shallow boundary {boundary}.')
                if result.returncode != 1:
                    result.check_returncode()
    return base_sha, head_sha


def added_unbounded_dependencies(repo: Path, base: str, head: str | None) -> list[str]:
    # Missing refs/history must never become an empty successful diff, as a shell
    # pipeline with `|| true` can permit.
    base_sha, head_sha = selected_history(repo, base, 'HEAD' if head is None else head)
    # A contributor's default check includes both staged and unstaged edits.
    # Explicit --head retains the exact committed comparison for diagnostics.
    comparison = git(repo, 'merge-base', base_sha, head_sha) if head is None else f'{base_sha}...{head_sha}'
    diff = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-color', '--unified=0',
               comparison, '--', 'pyproject.toml')
    return sorted({match.group(0) for line in diff.splitlines()
                   if line.startswith('+') and not line.startswith('+++')
                   for match in UNBOUNDED.finditer(line)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--base', default='origin/main')
    parser.add_argument('--head', help='Compare this committed head instead of current working files')
    args = parser.parse_args(argv)
    try:
        offenders = added_unbounded_dependencies(args.repo, args.base, args.head)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f'Dependency-bound history check failed: {detail}', file=sys.stderr)
        return 2
    if offenders:
        print('Added PyPI dependencies without upper bounds: ' + ', '.join(offenders), file=sys.stderr)
        print('Add a <next_major ceiling per CONTRIBUTING.md dependency policy.', file=sys.stderr)
        return 1
    print('No newly added unbounded PyPI dependencies.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
