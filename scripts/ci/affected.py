"""Conservative Python/E2E file selection for PR feedback, not qualification."""
from __future__ import annotations

import ast
import hashlib
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

# Do not map this helper to gateway directories: CLI, TUI, cron and ACP also
# reach it. Retain every file except the explicitly audited tooling probes.
SILENCE_SOURCE = 'gateway/response_filters.py'
SILENCE_COMPANION = 'tests/gateway/test_gateway_silence_tokens.py'
SILENCE_SOURCE_BLOB = '3e410cf58d11d2cea44120de92f541cca7bda46c'
SILENCE_TEST_BLOB = '834d7d11e03033d882abe18423c46e697d067ff9'
SILENCE_INDEPENDENT_TESTS = (
    'tests/ci/test_cache_report.py',
    'tests/ci/test_check_os_marker_fakes.py',
    'tests/ci/test_lockfile_diff.py',
    'tests/scripts/test_bash_shebangs.py',
    'tests/scripts/test_case_collision_check.py',
    'tests/scripts/test_check_agents_md_size.py',
    'tests/scripts/test_job_groups.py',
    'tests/scripts/test_run_tests_parallel.py',
)
# Audit at fbd104cd903. Include absent pytest entry points: adding one on
# main invalidates the proof just as changing a pinned fixture or tool does.
SILENCE_AUDIT_PATHS = (*SILENCE_INDEPENDENT_TESTS,
    'scripts/ci/cache_report.py', 'scripts/ci/check_os_marker_fakes.py',
    'scripts/ci/lockfile_diff.py', 'scripts/ci/pnpm_lock.py',
    'scripts/check_bash_shebangs.py', 'scripts/check-case-collisions.py',
    'scripts/ci/check_agents_md_size.py', 'scripts/releases/job_groups.py',
    'scripts/run_tests.sh', 'scripts/run_tests_parallel.py', 'scripts/run_tests_launch.py',
    'scripts/run_tests_fork_child.py', 'scripts/ci/list_os_marked_tests.py', 'tests/fixtures/pnpm_lock',
    'conftest.py', 'pytest.ini', '.pytest.ini', 'setup.cfg', 'tox.ini', 'pyproject.toml',
    'uv.lock', 'pm/lock.json', 'tests/__init__.py', 'tests/conftest.py', 'tests/ci/__init__.py',
    'tests/ci/conftest.py', 'tests/scripts/__init__.py', 'tests/scripts/conftest.py',
    'tests/_fixtures', 'tests/git_safety.py', 'tests/socket_paths.py', 'tests/home_io_guard.py',
    'gateway/__init__.py', 'gateway/reply_latency.py', 'gateway/reply_latency_sink.py', 'gateway/status.py',
    'hermes_agent/compat', 'hermes_cli/voice.py', 'tools/voice_mode.py', 'hermes_cli/plugins.py',
    'hermes_cli/gui_uninstall.py', 'agent/moa_loop.py', 'hermes_constants.py',
    'hermes_constants_scratch.py', 'hermes_state_guard.py',
)
SILENCE_AUDIT_TREE = 'a66f5cb464e1a1582d03d901e32c19cb438e68d306633dd54ba0fcfbef2e4907'

# Keep the edit within local string/tuple normalization, not new call sites,
# global writes, imports, or attribute/subscript writes.
SILENCE_BODY_NODES = (
    ast.Return, ast.Assign, ast.If, ast.IfExp, ast.Constant, ast.Name, ast.Load, ast.Store,
    ast.Call, ast.Attribute, ast.Subscript, ast.Slice, ast.Tuple, ast.Starred,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.BinOp, ast.Add,
    ast.GeneratorExp, ast.comprehension,
)
SILENCE_CALLS = {'isinstance', 'len', 'tuple', '_strip_edge_silence_punctuation', '_canonical_silence_candidate'}
SILENCE_STRING_METHODS = {'strip', 'lstrip', 'rstrip', 'upper', 'lower', 'startswith', 'endswith', 'join', 'split'}

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


def parsed_module(source: str) -> ast.Module:
    # Git uses surrogateescape for paths. Never treat undecodable source as
    # audited Python, even if the invalid bytes would only occur in a comment.
    content = source.encode('utf-8')
    compile(content, '<audited module>', 'exec', dont_inherit=True)
    return ast.parse(content, type_comments=True)


def module_contract(source: str) -> str:
    tree = parsed_module(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
            node.body = []
    return ast.dump(tree)


def normalization_node(node: ast.AST) -> bool:
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    if isinstance(node, ast.Assign):
        return all(isinstance(target, ast.Name) for target in node.targets)
    if isinstance(node, ast.comprehension):
        return isinstance(node.target, ast.Name) and not node.is_async
    if isinstance(node, ast.Call):
        fn = node.func
        return (isinstance(fn, ast.Name) and fn.id in SILENCE_CALLS
                or isinstance(fn, ast.Attribute) and fn.attr in SILENCE_STRING_METHODS)
    return isinstance(node, SILENCE_BODY_NODES)


def silence_source_contract(source: str) -> str:
    tree = parsed_module(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == '_canonical_silence_candidates':
            if not all(normalization_node(part) for stmt in node.body for part in ast.walk(stmt)):
                raise ValueError('silence normalization exceeds the audited operations')
            node.body = []
    return ast.dump(tree)


def declarative_test(node: ast.stmt) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith('test_'):
        return False
    args = node.args
    if node.returns or getattr(node, 'type_params', ()) or args.defaults or any(args.kw_defaults):
        return False
    if any(arg.annotation for arg in ast.walk(args) if isinstance(arg, ast.arg)):
        return False
    for decorator in node.decorator_list:
        if ast.unparse(decorator) == 'pytest.mark.asyncio':
            continue
        if not isinstance(decorator, ast.Call) or ast.unparse(decorator.func) != 'pytest.mark.parametrize':
            return False
        values = (*decorator.args, *(kw.value for kw in decorator.keywords if kw.arg == 'ids'))
        if len(values) != len(decorator.args) + len(decorator.keywords):
            return False
        literal_nodes = (ast.Constant, ast.List, ast.Tuple, ast.Load, ast.BinOp, ast.Add, ast.Mult)
        if not all(isinstance(part, literal_nodes) for value in values for part in ast.walk(value)):
            return False
    return True


def silence_test_contract(source: str) -> str:
    tree = parsed_module(source)
    # The shared module can gain regression cases, not imports, fixtures,
    # plugin registration or changes to its helpers.
    tree.body = [node for node in tree.body if not declarative_test(node)]
    return ast.dump(tree)


def matches_contract(git: Callable[..., str], audited: str, blobs: tuple[str, ...],
                     contract: Callable[[str], str]) -> bool:
    expected = contract(git('cat-file', 'blob', audited))
    return all(contract(git('cat-file', 'blob', blob)) == expected for blob in set(blobs) - {audited})


def silence_selection(git: Callable[..., str], base: str, sha: str, all_tests: tuple[str, ...],
                      changed: dict[str, tuple[str, ...]]) -> Selection:
    if not {SILENCE_COMPANION, *SILENCE_INDEPENDENT_TESTS} <= set(all_tests):
        return Selection(base, 'full', all_tests, 'audited silence inventory unavailable')
    tree = git('ls-tree', '-z', '--full-tree', sha, '--', *SILENCE_AUDIT_PATHS)
    if hashlib.sha256(tree.encode('utf-8')).hexdigest() != SILENCE_AUDIT_TREE:
        return Selection(base, 'full', all_tests, 'audited tooling tests or their inputs have changed')
    # Check the companion even if main already changed it, and not this PR.
    companion = changed.get(SILENCE_COMPANION)
    if companion is None:
        companion = (git('rev-parse', f'{sha}:{SILENCE_COMPANION}').strip(),)
        if not SHA.fullmatch(companion[0]):
            return Selection(base, 'full', all_tests, 'unreadable silence test blob')
    if not (matches_contract(git, SILENCE_SOURCE_BLOB, changed[SILENCE_SOURCE], silence_source_contract)
            and matches_contract(git, SILENCE_TEST_BLOB, companion, silence_test_contract)):
        return Selection(base, 'full', all_tests, 'diff exceeds the audited silence-marker scope')
    omitted = set(SILENCE_INDEPENDENT_TESTS)
    return Selection(base, 'affected', tuple(path for path in all_tests if path not in omitted),
                     'silence-marker edit; omit only audited independent tooling tests')


def select(git: Callable[..., str], sha: str, inventory: list[str],
           base_ref: str = 'refs/remotes/origin/main') -> Selection:
    """Narrow only recognized edits backed by an explicit audit."""
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
        if SILENCE_SOURCE in changed and changed.keys() <= {SILENCE_SOURCE, SILENCE_COMPANION}:
            return silence_selection(git, base, sha, all_tests, changed)
        if not changed.keys() <= eligible:
            return full('paths outside the audited edit scopes')
        for path, (before, after) in changed.items():
            if not matches_contract(git, AUDITED_LEAF_TESTS[path], (before, after), module_contract):
                return full('audited test module differs from its audited module structure')
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return full('Git could not read comparison history, paths or audited blobs')
    except (SyntaxError, ValueError, RecursionError):
        return full('unreadable Python or edit exceeds the audited operations')
    return Selection(base, 'affected', tuple(sorted(changed)), 'audited leaf test bodies only')
