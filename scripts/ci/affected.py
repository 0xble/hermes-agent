#!/usr/bin/env python3
"""Select the pytest files a change can affect, for the quick CI lane.

Selected: changed test files, every test file that imports a changed module
directly or through other modules, tests under a conftest.py that does, and
tests whose source names a changed file's path. Shared setup (conftest.py,
pytest or dependency configuration, lock files, CI configuration) selects the
whole suite. The quick lane is fast feedback only: the full suite still runs
once before merge, so a selection miss is caught there.

Usage: affected.py [--base REF] [--json]
Prints one test path or pytest node id per line, or ALL when the whole suite
must run.
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
    'pyproject.toml', '*/pyproject.toml', 'setup.cfg', 'setup.py', 'tox.ini', 'pytest.ini', '.pytest.ini',
    'uv.lock', '*/uv.lock', 'poetry.lock', '*/poetry.lock', 'requirements*.txt', '*/requirements*.txt',
    'package.json', '*/package.json', 'package-lock.json', '*/package-lock.json',
    'pnpm-lock.yaml', '*/pnpm-lock.yaml', 'pnpm-workspace.yaml', '.npmrc', '*/.npmrc',
    'bun.lock', '*/bun.lock', 'bun.lockb', '*/bun.lockb',
    'Cargo.toml', '*/Cargo.toml', 'Cargo.lock', '*/Cargo.lock', 'go.mod', '*/go.mod', 'go.sum', '*/go.sum',
    '.python-version', '.tool-versions', 'mise.toml', '.mise.toml',
    '.github/workflows/*', '.github/actions/*', '.github/known-flakes*', 'bin/ci', 'bin/ci-*', 'scripts/ci/*', 'ci/*', '.mergify.yml',
    'sitecustomize.py', '*/sitecustomize.py',
)
# Optional per-repo files kept beside this script.
EXTRA_FULL_SUITE_FILE = Path(__file__).with_name('full-suite-paths')  # one glob per line
# One rule per line: a regex matched against each changed path, then the tests
# or pytest node ids it selects; targets may use \1-style groups. For tests
# that discover inputs by globbing, which neither imports nor path names reveal.
PATH_MAP_FILE = Path(__file__).with_name('affected-map')
MIN_TEXT_TOKEN = 8  # shorter basenames match too much prose to be a signal
# Basenames shared by many files say nothing about which one a test reads.
GENERIC_NAMES = {'__init__.py', '__main__.py', 'conftest.py', 'README.md', 'SKILL.md', 'AGENTS.md',
                 'CLAUDE.md', 'config.yaml', 'settings.json', 'package.json', 'index.ts', 'index.js'}


GIT_TIMEOUT_SECONDS = 120


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                          text=True, encoding='utf-8', errors='replace',
                          timeout=GIT_TIMEOUT_SECONDS).stdout


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith('.py') and (name.startswith('test_') or name.endswith('_test.py'))


def testpaths(root: Path) -> list[str]:
    try:
        data = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8-sig'))
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
    lines = [ln.strip() for ln in extra.read_text(encoding='utf-8-sig').splitlines()]
    return FULL_SUITE_PATTERNS + tuple(ln for ln in lines if ln and not ln.startswith('#'))


def mapped_targets(changed: list[str]) -> set[str]:
    if not PATH_MAP_FILE.is_file():
        return set()
    targets: set[str] = set()
    for line in PATH_MAP_FILE.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        pattern, *outputs = line.split()
        regex = re.compile(pattern)
        for path in changed:
            if match := regex.match(path):
                targets.update(match.expand(output) for output in outputs)
    return targets


def module_names(path: str) -> list[str]:
    """Every dotted name the file could be imported as, longest first."""
    parts = list(PurePosixPath(path).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return ['.'.join(parts[i:]) for i in range(len(parts))] if parts else []


def walk(tree: ast.AST, lazy: bool):
    """Yield nodes that run at import time; with lazy, also function bodies.

    A production module's function-local import is deferred on purpose and
    only runs if that function does, so it is not followed. Tests, conftest
    files and test helpers import inside test bodies and fixtures, so every
    import there counts.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and ast.unparse(child.test) in ('TYPE_CHECKING', 'typing.TYPE_CHECKING'):
                stack.extend(child.orelse)  # annotation-only imports never run
                continue
            if lazy or not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append(child)


def is_test_side(path: str) -> bool:
    """Tests, conftest.py and helpers under a tests folder: their deferred imports run too."""
    pure = PurePosixPath(path)
    return is_test_file(path) or pure.name == 'conftest.py' or bool({'tests', 'test'} & set(pure.parts[:-1]))


def module_strings(tree: ast.Module) -> dict[str, list[str]]:
    """Module-level names bound to a string or a list/tuple of strings."""
    values: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
            strings = [item.value for item in items if isinstance(item, ast.Constant) and isinstance(item.value, str)]
            if strings and len(strings) == len(items):
                values[node.targets[0].id] = strings
    return values


def from_import_names(node: ast.ImportFrom, package: list[str]) -> set[str]:
    if node.level:
        base_parts = package[:len(package) - (node.level - 1)] if node.level > 1 else package
        base = '.'.join([*base_parts, *(node.module.split('.') if node.module else [])])
    else:
        base = node.module or ''
    names = {f'{base}.{alias.name}' if base else alias.name for alias in node.names}
    return names | ({base} if base else set())


def dynamic_import_names(node: ast.Call, strings: dict[str, list[str]]) -> list[str]:
    """import_module, __import__ and pytest.importorskip with a literal or a module constant."""
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
    if name not in ('import_module', '__import__', 'importorskip') or not node.args:
        return []
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    return strings.get(arg.id, []) if isinstance(arg, ast.Name) else []


def imports(path: str, source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    package = list(PurePosixPath(path).parent.parts)
    strings = module_strings(tree)
    found: set[str] = set(strings.get('pytest_plugins', []))
    for node in walk(tree, lazy=is_test_side(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found |= from_import_names(node, package)
        elif isinstance(node, ast.Call):
            found.update(dynamic_import_names(node, strings))
    return found


def changed_paths(root: Path, base: str) -> list[str]:
    diff = git(root, 'diff', '--name-only', '--no-renames', '-z', f'{base}...HEAD', '--')
    return sorted({p for p in diff.split('\0') if p})


def shared_setup_change(changed: list[str]) -> str | None:
    """The first changed path that can alter how every test runs, if any."""
    patterns = full_suite_patterns()
    for path in changed:
        if path.endswith('.md'):
            continue  # prose beside CI configuration never changes how tests run
        if any(fnmatch.fnmatchcase(path, pat) for pat in patterns):
            return path
    return None


def load_sources(root: Path) -> dict[str, str]:
    sources: dict[str, str] = {}
    for path in (p for p in git(root, 'ls-files', '-z', '--', '*.py').split('\0') if p):
        try:
            sources[path] = (root / path).read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            continue
    return sources


def import_graph(sources: dict[str, str], tests: list[str], changed: list[str]) -> dict[str, set[str]]:
    """Map each file to the files that import it, including conftest scope."""
    # Module name -> files. Deleted modules keep their names so importers still match.
    by_name: dict[str, set[str]] = defaultdict(set)
    for path in [*sources, *(p for p in changed if p.endswith('.py') and p not in sources)]:
        for name in module_names(path):
            by_name[name].add(path)
    importers: dict[str, set[str]] = defaultdict(set)
    for path, source in sources.items():
        for name in imports(path, source):
            # "from a.b import c" may name module a.b.c or attribute c of a.b.
            targets = set(by_name.get(name, ()) or by_name.get(name.rpartition('.')[0], ()))
            # Importing a.b.c also runs the package initializers a and a.b.
            parts = name.split('.')
            for depth in range(1, len(parts)):
                targets.update(t for t in by_name.get('.'.join(parts[:depth]), ()) if t.endswith('__init__.py'))
            for target in targets - {path}:
                importers[target].add(path)
    # A test depends on every conftest.py above it.
    for conftest in (p for p in sources if PurePosixPath(p).name == 'conftest.py'):
        folder = str(PurePosixPath(conftest).parent)
        importers[conftest].update(t for t in tests if folder == '.' or t.startswith(folder + '/'))
    return importers


def tests_reaching(starts: list[str], importers: dict[str, set[str]], tests: set[str]) -> set[str]:
    """Tests among the starts or among their direct and transitive importers."""
    seen = set(starts)
    queue = deque(starts)
    while queue:
        current = queue.popleft()
        for parent in importers.get(current, ()):
            if parent not in seen:
                seen.add(parent)
                queue.append(parent)
    return seen & tests


def path_reference_pattern(changed: list[str]) -> re.Pattern[str]:
    """Match a changed file's path, its last 2 parts, or a distinctive basename."""
    tokens = set()
    for path in changed:
        pure = PurePosixPath(path)
        tokens.add(path)
        if len(pure.parts) > 1:
            tokens.add('/'.join(pure.parts[-2:]))
        if len(pure.name) >= MIN_TEXT_TOKEN and pure.name not in GENERIC_NAMES:
            tokens.add(pure.name)
    return re.compile('|'.join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)))


def select(root: Path, base: str) -> tuple[str, list[str], str]:
    """Return (mode, tests, reason). mode is 'full' or 'affected'."""
    changed = changed_paths(root, base)
    if not changed:
        return 'affected', [], 'no changes against base'
    if shared := shared_setup_change(changed):
        return 'full', [], f'shared setup changed: {shared}'
    sources = load_sources(root)
    roots = testpaths(root)
    tests = sorted(p for p in sources if is_test_file(p) and under(p, roots))
    importers = import_graph(sources, tests, changed)
    test_set = set(tests)
    affected = tests_reaching([p for p in changed if p.endswith('.py')], importers, test_set)
    # Files that name a changed file (fixtures, data, CLIs run by path), and
    # the tests that import such a helper.
    pattern = path_reference_pattern(changed)
    naming = [path for path, source in sources.items() if pattern.search(source)]
    affected |= tests_reaching(naming, importers, test_set)
    affected |= mapped_targets(changed)
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
    except subprocess.TimeoutExpired:
        return 'full', [], 'git timed out'
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
