"""Conservative Python/E2E file selection for PR feedback, not qualification."""
from __future__ import annotations

import ast
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Audited at 06b13de1c77: standalone assertions, no fixture/plugin exports or
# repo code importing this module. Re-audit before adding paths or making an
# entry shared. Being collected by pytest is not evidence of being private.
AUDITED_LEAF_TESTS = {
    'tests/gateway/test_response_filters.py': 'a77abdc539c9fa9e4336ab09e7a3a2156c47ae65',
}
SHA = re.compile(r'[0-9a-f]{40}')
REGULAR_MODIFICATION = re.compile(r':100644 100644 ([0-9a-f]{40}) ([0-9a-f]{40}) M')


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


def module_contract(source: str) -> str:
    # Git uses surrogateescape for paths. Never treat undecodable source as
    # audited Python, even if the invalid bytes would only occur in a comment.
    content = source.encode('utf-8')
    compile(content, '<audited test>', 'exec', dont_inherit=True)
    tree = ast.parse(content, type_comments=True)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
            node.body = []
    return ast.dump(tree)


def select(git: Callable[..., str], sha: str, inventory: list[str],
           base_ref: str = 'refs/remotes/origin/main') -> Selection:
    """Narrow only edits inside existing test bodies in explicitly audited modules."""
    all_tests = tuple(sorted(set(inventory)))
    eligible = AUDITED_LEAF_TESTS.keys() & set(all_tests)
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
        raw = git('diff', '--raw', '-z', '--no-abbrev', '--no-renames', '--no-ext-diff',
                  '--no-relative', '--ignore-submodules=none', base, sha, '--')
        if not raw:
            return full('empty diff')
        fields = raw.split('\0')
        if fields.pop() or len(fields) % 2:
            return full('unreadable diff')
        changed = {}
        for change, path in zip(fields[::2], fields[1::2], strict=True):
            blobs = REGULAR_MODIFICATION.fullmatch(change)
            if not blobs or not path or path in changed:
                return full('addition, deletion, rename, mode change or unreadable diff')
            changed[path] = blobs.groups()
        if not changed.keys() <= eligible:
            return full('path outside the audited leaf-test allowlist')
        for path, (before, after) in changed.items():
            audited = AUDITED_LEAF_TESTS[path]
            contract = module_contract(git('cat-file', 'blob', audited))
            for blob in {before, after} - {audited}:
                if module_contract(git('cat-file', 'blob', blob)) != contract:
                    return full('audited test module differs from its audited module structure')
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return full('Git could not read comparison history, paths or test blobs')
    except (SyntaxError, ValueError, RecursionError):
        return full('could not parse an audited test module')
    return Selection(base, 'affected', tuple(sorted(changed)), 'audited leaf test bodies only')
