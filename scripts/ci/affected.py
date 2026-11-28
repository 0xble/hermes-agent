#!/usr/bin/env python3
"""Select the pytest files a change can affect, for the quick CI lane.

Selected: changed test files, every test file that imports a changed module
directly or through other modules, tests under a conftest.py that does, and
tests whose source names a changed file's path. Shared setup (conftest.py,
pytest or dependency configuration, lock files, CI configuration) selects the
whole suite. The quick lane is fast feedback only: the full suite still runs
once before merge, so a selection miss is caught there.

Usage: affected.py [--base REF] [--json]
Prints one test path per line, or ALL when the whole suite must run.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath

# Paths whose change can alter how every test runs.
FULL_SUITE_PATTERNS = (
    'conftest.py', '*/conftest.py',
    'pyproject.toml', 'setup.cfg', 'setup.py', 'tox.ini', 'pytest.ini', '.pytest.ini',
    'uv.lock', 'poetry.lock', 'requirements*.txt', '*/requirements*.txt',
    'package.json', 'package-lock.json', 'pnpm-lock.yaml', 'bun.lock', 'bun.lockb',
    '.python-version', '.tool-versions', 'mise.toml', '.mise.toml',
    '.github/workflows/*', '.github/actions/*', '.github/known-flakes*', 'bin/ci', 'bin/ci-*', 'scripts/ci/*', 'ci/*', '.mergify.yml',
    'sitecustomize.py', '*/sitecustomize.py',
)
# Optional per-repo glob list kept beside this script.
EXTRA_FULL_SUITE_FILE = Path(__file__).with_name('full-suite-paths')
MIN_TEXT_TOKEN = 8  # shorter basenames match too much prose to be a signal
# Basenames shared by many files say nothing about which one a test reads.
GENERIC_NAMES = {'__init__.py', '__main__.py', 'conftest.py', 'README.md', 'SKILL.md', 'AGENTS.md',
                 'CLAUDE.md', 'config.yaml', 'settings.json', 'package.json', 'index.ts', 'index.js'}


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith('.py') and (name.startswith('test_') or name.endswith('_test.py'))


def testpaths(root: Path) -> list[str]:
    try:
        data = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
        paths = data.get('tool', {}).get('pytest', {}).get('ini_options', {}).get('testpaths')
    except (OSError, tomllib.TOMLDecodeError):
        paths = None
    return [p.rstrip('/') for p in paths] if paths else ['']


def under(path: str, roots: list[str]) -> bool:
    return any(not r or path == r or path.startswith(r + '/') for r in roots)


def full_suite_patterns() -> tuple[str, ...]:
    extra = EXTRA_FULL_SUITE_FILE
    if not extra.is_file():
        return FULL_SUITE_PATTERNS
    lines = [ln.strip() for ln in extra.read_text(encoding='utf-8').splitlines()]
    return FULL_SUITE_PATTERNS + tuple(ln for ln in lines if ln and not ln.startswith('#'))


def module_names(path: str) -> list[str]:
    """Every dotted name the file could be imported as, longest first."""
    parts = list(PurePosixPath(path).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return ['.'.join(parts[i:]) for i in range(len(parts))] if parts else []


def walk(tree: ast.AST, lazy: bool):
    """Yield nodes that run at import time; with lazy, also function bodies.

    A production module's function-local import is deferred on purpose and
    only runs if that function does, so it is not followed. Tests import
    inside test bodies and fixtures, so every import in a test file counts.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and 'TYPE_CHECKING' in ast.unparse(child.test):
                stack.extend(child.orelse)  # annotation-only imports never run
                continue
            if lazy or not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append(child)


def imports(path: str, source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    package = list(PurePosixPath(path).parent.parts)
    found: set[str] = set()
    for node in walk(tree, lazy=is_test_file(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                base = '.'.join([*base_parts, *(node.module.split('.') if node.module else [])])
            else:
                base = node.module or ''
            if base:
                found.add(base)
            found.update(f'{base}.{alias.name}' if base else alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            # importlib.import_module("pkg.mod") and __import__("pkg.mod")
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
            if name in ('import_module', '__import__') and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
    return found


def select(root: Path, base: str) -> tuple[str, list[str], str]:
    """Return (mode, tests, reason). mode is 'full' or 'affected'."""
    diff = git(root, 'diff', '--name-only', '--no-renames', '-z', f'{base}...HEAD', '--')
    changed = sorted({p for p in diff.split('\0') if p})
    if not changed:
        return 'affected', [], 'no changes against base'
    patterns = full_suite_patterns()
    for path in changed:
        if any(fnmatch.fnmatchcase(path, pat) for pat in patterns):
            return 'full', [], f'shared setup changed: {path}'

    roots = testpaths(root)
    tracked = [p for p in git(root, 'ls-files', '-z', '--', '*.py').split('\0') if p]
    sources: dict[str, str] = {}
    for path in tracked:
        try:
            sources[path] = (root / path).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
    tests = sorted(p for p in sources if is_test_file(p) and under(p, roots))

    # Module name -> files. Deleted modules keep their names so importers still match.
    by_name: dict[str, set[str]] = defaultdict(set)
    for path in [*sources, *(p for p in changed if p.endswith('.py') and p not in sources)]:
        for name in module_names(path):
            by_name[name].add(path)

    importers: dict[str, set[str]] = defaultdict(set)
    for path, source in sources.items():
        for name in imports(path, source):
            parts = name.split('.')
            # "from a.b import c" may name module a.b.c or attribute c of a.b.
            for target in by_name.get(name, ()) or by_name.get('.'.join(parts[:-1]), ()):
                if target != path:
                    importers[target].add(path)
    # A test depends on every conftest.py above it.
    conftests = [p for p in sources if PurePosixPath(p).name == 'conftest.py']
    for conftest in conftests:
        folder = str(PurePosixPath(conftest).parent)
        for test in tests:
            if folder == '.' or test.startswith(folder + '/'):
                importers[conftest].add(test)

    affected: set[str] = set()
    queue = deque(p for p in changed if p.endswith('.py'))
    seen = set(queue)
    while queue:
        current = queue.popleft()
        if current in tests:
            affected.add(current)
        for parent in importers.get(current, ()):
            if parent not in seen:
                seen.add(parent)
                queue.append(parent)

    # Tests that name a changed file (fixtures, data, CLIs run by path).
    tokens = set()
    for path in changed:
        tokens.add(path)
        pure = PurePosixPath(path)
        if len(pure.parts) > 1:
            tokens.add('/'.join(pure.parts[-2:]))
        if len(pure.name) >= MIN_TEXT_TOKEN and pure.name not in GENERIC_NAMES:
            tokens.add(pure.name)
    if tokens:
        pattern = re.compile('|'.join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)))
        for test in tests:
            if test not in affected and pattern.search(sources[test]):
                affected.add(test)
        # A helper module that names the file pulls in its importers too.
        for path, source in sources.items():
            if path not in tests and not is_test_file(path) and pattern.search(source):
                stack = [path]
                while stack:
                    node = stack.pop()
                    for parent in importers.get(node, ()):
                        if parent in tests:
                            affected.add(parent)
                        elif parent not in seen:
                            seen.add(parent)
                            stack.append(parent)

    return 'affected', sorted(affected), f'{len(changed)} changed files'


def resolve_base(root: Path, ref: str | None) -> str:
    candidates = [ref] if ref else ['origin/main', 'origin/master', 'main']
    for candidate in candidates:
        try:
            return git(root, 'merge-base', candidate, 'HEAD').strip()
        except subprocess.CalledProcessError:
            continue
    raise LookupError(f'no merge base with {", ".join(candidates)}')


def choose(root: Path, base_ref: str | None = None) -> tuple[str, list[str], str]:
    """Selection against the merge base, falling back to the full suite."""
    try:
        if git(root, 'rev-parse', '--is-shallow-repository').strip() == 'true':
            return 'full', [], 'shallow clone has no merge base'
        return select(root, resolve_base(root, base_ref))
    except subprocess.CalledProcessError as exc:
        return 'full', [], f'git failed: {exc.stderr.strip()[:200]}'
    except LookupError as exc:
        return 'full', [], str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base', help='base ref (default: origin/main)')
    parser.add_argument('--json', action='store_true', help='print a JSON summary instead of paths')
    args = parser.parse_args(argv)
    root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel').strip())
    mode, tests, reason = choose(root, args.base)
    if args.json:
        print(json.dumps({'mode': mode, 'reason': reason, 'tests': tests}))
    elif mode == 'full':
        print('ALL')
    else:
        print('\n'.join(tests))
    print(f'affected: {mode}, {len(tests) if mode == "affected" else "all"} test files ({reason})', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
