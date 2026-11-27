"""Conservative Python/E2E file selection for PR feedback, not qualification."""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Pilot one production file. Its consumers span direct and transitive callers
# (gateway, agent, cron, CLI, TUI and tools); keep all PR-safe E2E for it.
SILENCE_SOURCE = 'gateway/response_filters.py'
SILENCE_TEST_ROOTS = ('tests/agent/', 'tests/cron/', 'tests/e2e/', 'tests/gateway/',
                      'tests/hermes_cli/', 'tests/tools/', 'tests/tui_gateway/')
SHA = re.compile(r'[0-9a-f]{40}')


@dataclass(frozen=True)
class Selection:
    base: str | None
    selection: str
    tests: tuple[str, ...]
    reason: str

    def write(self, path: Path, sha: str) -> None:
        with path.open('x', encoding='utf-8') as stream:
            json.dump({'profile': 'affected', 'sha': sha, 'base': self.base,
                       'selection': self.selection, 'tests': self.tests, 'reason': self.reason}, stream)
            stream.write('\n')


def select(git: Callable[..., str], sha: str, inventory: list[str],
           base_ref: str = 'refs/remotes/origin/main') -> Selection:
    """Read NUL-delimited Git paths; any uncertainty selects the full inventory."""
    all_tests = tuple(sorted(set(inventory)))
    candidates = set(all_tests)
    base = None

    def full(reason: str) -> Selection:
        return Selection(base, 'full', all_tests, reason)

    if not all_tests:
        raise RuntimeError('Empty canonical Python/E2E inventory')
    try:
        if git('rev-parse', '--is-shallow-repository').strip() != 'false':
            return full('shallow history')
        bases = git('merge-base', '--all', base_ref, sha).splitlines()
        if len(bases) != 1 or not SHA.fullmatch(bases[0]):
            return full('no unique merge base')
        base = bases[0]
        raw = git('diff', '--name-status', '-z', '--no-renames', '--no-ext-diff', base, sha, '--')
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return full('Git could not resolve the merge base or diff')
    if not raw:
        return full('empty diff')
    fields = raw.split('\0')
    if fields.pop() or len(fields) % 2:
        return full('unreadable diff')
    changed = set()
    for status, path in zip(fields[::2], fields[1::2], strict=True):
        if status not in {'A', 'M'} or not path:
            return full('deletion, rename, type change or unknown Git status')
        changed.add(path)
    if changed <= candidates:
        return Selection(base, 'affected', tuple(sorted(changed)), 'runnable test files only')
    if SILENCE_SOURCE in changed and all(
        path == SILENCE_SOURCE or (path in candidates and path.startswith(SILENCE_TEST_ROOTS))
        for path in changed
    ):
        tests = tuple(p for p in all_tests if p.startswith(SILENCE_TEST_ROOTS)
                      or (p.startswith('tests/') and p.count('/') == 1)
                      or p.startswith('candidate-extensions/'))
        if 'tests/gateway/test_response_filters.py' in tests:
            return Selection(base, 'affected', tests, 'response filter pilot: consumer suites and PR-safe E2E')
    return full('mixed changes or paths outside the pilot')
