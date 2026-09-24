#!/usr/bin/env python3
"""Portable source CI. No publisher credentials, runtime plugins, or personal setup."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / '.ci'
PINS = json.loads((ROOT / 'scripts/ci/toolchain.json').read_text(encoding='utf-8'))
EXTRAS = ('all', 'dev', 'anthropic', 'bedrock', 'mistral', 'fal', 'modal', 'daytona', 'hindsight', 'parallel-web')
LANES = {
    'static': 'Blocking lint, source policies, attribution, history and lock consistency',
    'python': 'Canonical full tests (excludes integration/e2e/docker)',
    'e2e': 'Canonical isolated tests/e2e, without external integration opt-ins',
    'node': 'All nine nonrelease workspace checks plus runner regression tests',
    'docs': 'Snapshot generation parity, links, diagrams and English site build',
    'rust': 'Bootstrap installer cargo test --lib on the actual host',
    'container-lint': 'Dockerfile hadolint and docker/ shellcheck (no container execution)',
}
OPTIONAL_LANES = {
    'native-os': 'Partial: actual macOS/Windows marked tests, plus both Windows installer shells',
}

# Keep the existing full/check interface for maintainers. Hosted CI selects these
# explicit profiles rather than treating partial --lane runs as qualification.
GATE_PYTHON_FILES = (
    'tests/agent/test_agent_guardrails.py',
    'tests/agent/test_oneshot.py',
    'tests/gateway/test_own_policy_startup_gate.py',
    'tests/hermes_cli/test_cli_retry.py',
)
GATE_LANES = ('static', 'python-gate', 'node-gate')


def assert_exact_checkout(expected: str) -> None:
    if not re.fullmatch(r'[0-9a-f]{40}', expected):
        raise RuntimeError('Expected a full lowercase 40-character commit SHA')
    actual = git('rev-parse', 'HEAD').strip()
    if actual != expected:
        raise RuntimeError(f'Checkout SHA mismatch: expected {expected}, got {actual}')
    if subprocess.run(['git', 'diff', '--quiet', '--exit-code'], cwd=ROOT).returncode != 0 or \
       subprocess.run(['git', 'diff', '--cached', '--quiet', '--exit-code'], cwd=ROOT).returncode != 0:
        raise RuntimeError('Tracked checkout differs from the committed SHA')


def preflight() -> None:
    # No installs or credential-bearing runtime: fast developer feedback.
    run([sys.executable, '-m', 'unittest',
         'scripts.ci.tests.test_portable.PortableGateTests.test_exact_checkout_rejects_malformed_wrong_and_mutated_sha'])
    run([sys.executable, '-m', 'py_compile', 'scripts/ci/portable.py'])



def run(argv: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print('+ ' + ' '.join(map(str, argv)), flush=True)
    subprocess.run(argv, cwd=cwd, env=env, check=True)


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(['git', *args], cwd=cwd).decode('utf-8', errors='surrogateescape')


def source_files(root: Path = ROOT) -> list[str]:
    # Include unstaged/new source for contributor checks, but never dependency/build state.
    return sorted(set(git('ls-files', '-z', '--cached', '--others', '--exclude-standard', cwd=root).split('\0')) - {''})


def fingerprints(root: Path, paths: list[str]) -> dict[str, str]:
    result = {}
    for relative in paths:
        path = root / relative
        if path.is_symlink():
            result[relative] = 'link:' + os.readlink(path)
        elif path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@contextmanager
def source_unchanged() -> Iterator[None]:
    paths = source_files()
    before = fingerprints(ROOT, paths)
    try:
        yield
    finally:
        after = fingerprints(ROOT, paths)
        changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
        if changed:
            raise RuntimeError('CI modified source files: ' + ', '.join(changed))


@contextmanager
def external_temporary_directory(prefix: str, parent: Path | None = None) -> Iterator[Path]:
    """Own one temporary directory without repository/dependency ancestry."""
    parent = (parent if parent is not None else Path(tempfile.gettempdir())).resolve()
    for ancestor in (parent, *parent.parents):
        if (ancestor / '.git').exists() or (ancestor / 'node_modules').exists():
            raise RuntimeError(f'Temporary directory parent has Git or Node dependency ancestry: {parent}')
    with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as temporary:
        yield Path(temporary)


def environment(home: Path) -> dict[str, str]:
    # Allowlist location variables only. No API keys, NODE_OPTIONS, pytest selectors,
    # npm user config, git credentials, or personal Hermes plugin directories.
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT') if key in os.environ}
    env.update({
        'HOME': str(home), 'USERPROFILE': str(home),
        'PATH': str(ROOT / '.venv' / ('Scripts' if os.name == 'nt' else 'bin')) + os.pathsep + env.get('PATH', ''),
        'TMPDIR': str(home / 'tmp'), 'TEMP': str(home / 'tmp'), 'TMP': str(home / 'tmp'),
        'XDG_CACHE_HOME': str(STATE / 'cache'), 'XDG_CONFIG_HOME': str(home / 'config'),
        'UV_CACHE_DIR': str(STATE / 'cache/uv'), 'UV_PYTHON_INSTALL_DIR': str(STATE / 'python'),
        'UV_PROJECT_ENVIRONMENT': str(ROOT / '.venv'),
        'npm_config_cache': str(STATE / 'cache/npm'), 'npm_config_userconfig': str(home / 'npmrc'),
        'CARGO_HOME': str(STATE / 'cargo'), 'CARGO_TARGET_DIR': str(STATE / 'cargo-target'),
        'CARGO_BUILD_JOBS': '2',
        'TZ': 'UTC', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONHASHSEED': '0',
        'PYTHONUTF8': '1', 'CI': 'true', 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': (home / 'gitconfig').resolve().as_posix(),
    })
    for directory in ('tmp', 'config'):
        (home / directory).mkdir(parents=True, exist_ok=True)
    (home / 'gitconfig').write_text(
        f'[safe]\n\tdirectory = {ROOT.resolve().as_posix()}\n', encoding='utf-8'
    )
    return env


def require_tools(names: tuple[str, ...], env: dict[str, str]) -> None:
    for name in names:
        output = subprocess.check_output([name, '--version'], env=env, text=True, encoding='utf-8', errors='replace')
        match = re.search(r'(?<!\d)(\d+\.\d+\.\d+)\b', output)
        if not match or match.group(1) != PINS[name]:
            raise RuntimeError(f'{name}: require {PINS[name]}, found {output.strip()} (see scripts/ci/toolchain.json)')


def python(env: dict[str, str]) -> str:
    executable = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not executable.is_file():
        raise RuntimeError('Checkout Python environment missing. Run bin/ci setup first.')
    output = subprocess.check_output([str(executable), '--version'], env=env, text=True, encoding='utf-8', errors='replace').strip()
    if output != f"Python {PINS['python']}":
        raise RuntimeError(f"Require checkout Python {PINS['python']}, found {output}. Run bin/ci setup.")
    return str(executable)


def aggregate(actions: list[tuple[str, Callable[[], None]]]) -> bool:
    failed = []
    for name, action in actions:
        print(f'\n=== {name} ===', flush=True)
        try:
            action()
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            print(f'FAIL {name}: {error}', file=sys.stderr, flush=True)
            failed.append(name)
    if failed:
        print('Failed: ' + ', '.join(failed), file=sys.stderr)
    return not failed


def setup(env: dict[str, str]) -> None:
    require_tools(('uv', 'node', 'npm'), env)
    run(['uv', 'sync', '--locked', '--python', PINS['python'], *[v for extra in EXTRAS for v in ('--extra', extra)]], env=env)
    run(['npm', 'ci', '--no-audit', '--no-fund'], env=env)
    run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=ROOT / 'website', env=env)
    run(['uv', 'venv', '--python', PINS['python'], str(STATE / 'docs-venv')], env=env)
    run(['uv', 'pip', 'install', '--python', str(docs_python()), 'ascii-guard==2.3.0', 'pyyaml==6.0.3'], env=env)


def docs_python() -> Path:
    return STATE / 'docs-venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def history_policy() -> None:
    spec = importlib.util.spec_from_file_location('dependency_bounds', ROOT / 'scripts/ci/check_dependency_bounds.py')
    bounds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bounds)
    try:
        bounds.selected_history(ROOT, 'origin/main', 'HEAD')
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    base = git('merge-base', 'origin/main', 'HEAD').strip()
    if not base:
        raise RuntimeError('No common ancestor with origin/main')
    emails = git('log', f'{base}..HEAD', '--format=%ae', '--no-merges').splitlines()
    legacy = (ROOT / 'scripts/release.py').read_text(encoding='utf-8')
    missing = []
    for email in sorted(set(emails)):
        if any(token in email for token in ('teknium', 'noreply@github.com', 'dependabot', 'github-actions', 'anthropic.com', 'cursor.com')):
            continue
        if re.search(r'\+.*@users\.noreply\.github\.com', email):
            continue
        if not (ROOT / 'contributors/emails' / email).is_file() and f'"{email}"' not in legacy:
            missing.append(email)
    if missing:
        raise RuntimeError('Missing contributor mappings: ' + ', '.join(missing))
    offenders = [p for p in git('ls-files', '-z').split('\0')
                 if re.search(r'(^|/)(infograph|infograf)[^/]*/', p, re.I)
                 and re.search(r'\.(png|jpe?g|webp|gif)$', p, re.I)]
    if offenders:
        raise RuntimeError('Tracked PR infographics: ' + ', '.join(offenders))


def static(env: dict[str, str]) -> None:
    require_tools(('uv',), env)
    py = python(env)
    commands = [
        [py, '-m', 'unittest', 'discover', '-s', 'scripts/ci/tests', '-p', 'test_*.py'],
        [py, '-m', 'ruff', 'check', '.'],
        [py, 'scripts/check-windows-footguns.py', '--all'],
        [py, 'scripts/check_compat_pointers.py'],
        [py, 'scripts/check_no_tmp_literals.py'],
        [py, 'scripts/ci/check_os_marker_fakes.py'],
        [py, 'scripts/check-case-collisions.py'],
        [py, 'scripts/ci/check_profile_archive_boundary.py'],
        [py, 'scripts/ci/check_dependency_bounds.py', '--repo', '.'],
        [py, 'scripts/check_fork_patches.py', '--repo', '.', '--source-only'],
        [py, 'scripts/validate_plugin_catalog.py', 'plugin-catalog/'],
        [py, 'scripts/ci/check_plugin_admission.py'],
        ['uv', 'lock', '--check'],
    ]
    actions = [('history/attribution/infographics', history_policy)]
    actions += [(' '.join(command[1:]), lambda command=command: run(command, env=env)) for command in commands]
    if not aggregate(actions):
        raise RuntimeError('Blocking static checks failed')


def python_tests(env: dict[str, str], roots: list[str], workers: int,
                 pytest_args: list[str] | None = None) -> None:
    py = python(env)
    require_tools(('rg',), env)
    env = dict(env)
    env['HERMES_PYTHON'] = py
    command = ['bash', 'scripts/run_tests.sh', '-j', str(workers), '--file-retries', '0',
               *roots, *(pytest_args or [])]
    # The runner provides a disk-backed original HOME outside the checkout.
    # Keep large Python fixtures there, separate from isolated child HOME and
    # standard TMPDIR, which may be a small tmpfs in the read-only sandbox.
    if sys.platform.startswith('linux') and not os.access('/var/tmp', os.W_OK):  # no-tmp: ok - probe canonical scratch mount
        with external_temporary_directory('pt-', parent=Path.home()) as scratch:
            env['HERMES_TEST_SCRATCH_ROOT'] = str(scratch)
            run(command, env=env)
    else:
        run(command, env=env)


def native_os(env: dict[str, str], workers: int) -> None:
    marker = {'darwin': 'macos_only', 'win32': 'windows_only'}.get(sys.platform)
    if marker is None:
        raise RuntimeError('native-os requires an actual macOS or Windows host.')
    selected = subprocess.check_output(
        [python(env), 'scripts/ci/list_os_marked_tests.py', marker],
        cwd=ROOT, env=env, text=True, encoding='utf-8', errors='replace',
    )
    files = [line.strip() for line in selected.splitlines() if line.strip()]
    if not files:
        raise RuntimeError(f'No files selected for {marker}. Refusing an empty native OS lane.')
    actions = [(marker, lambda: python_tests(env, files, workers, pytest_args=['-m', f'{marker} and not integration']))]
    if sys.platform == 'win32':
        for shell in ('powershell', 'pwsh'):
            for case in ('longpath', 'node-compatibility', 'uv-shim-validation'):
                command = [shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                           f'scripts/tests/test-install-ps1-{case}.ps1']
                actions.append((f'{shell} installer {case}', lambda command=command: run(command, env=env)))
    if not aggregate(actions):
        raise RuntimeError('Native OS checks failed.')


def node_gate(env: dict[str, str], workers: int) -> None:
    # Admit fast cross-workspace checks on every PR; the full nine-unit Node
    # profile, including desktop UI and TUI suites, runs in nightly.
    python(env)
    require_tools(('node', 'npm'), env)
    run(['node', '--test', 'scripts/ci/tests/workspace-checks.test.mjs'], env=env)
    command = ['node', 'scripts/run-workspace-checks.mjs', '--concurrency', str(workers),
               '--skip', 'check:test:ui', '--skip', 'check:test:desktop:all',
               '--skip', 'ui-tui/packages/hermes-ink', '--skip', 'ui-tui :: check']
    if sys.platform.startswith('linux'):
        command = ['xvfb-run', '-a', *command]
    run(command, env=env)


def node(env: dict[str, str], workers: int) -> None:
    python(env)  # JavaScript tests spawn Python subprocesses from PATH.
    require_tools(('node', 'npm'), env)
    run(['node', '--test', 'scripts/ci/tests/workspace-checks.test.mjs'], env=env)
    command = ['node', 'scripts/run-workspace-checks.mjs', '--concurrency', str(workers), '--skip', 'check:test:desktop:all']
    if sys.platform.startswith('linux'):
        # A display is required, not an excuse to silently skip Electron tests.
        command = ['xvfb-run', '-a', *command]
    run(command, env=env)


def docs_inventory(root: Path) -> dict[str, str]:
    paths = []
    for relative in ('website/docs', 'website/sidebars.ts', 'website/i18n'):
        path = root / relative
        paths.extend(str(p.relative_to(root)) for p in path.rglob('*') if p.is_file()) if path.is_dir() else paths.append(relative)
    return fingerprints(root, paths)


def check_docs_parity(before: dict[str, str], after: dict[str, str]) -> None:
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    if changed:
        raise RuntimeError('Generated docs differ (run generators and commit): ' + ', '.join(changed))


def docs(env: dict[str, str]) -> None:
    require_tools(('node', 'npm'), env)
    if not docs_python().is_file():
        raise RuntimeError('Docs Python environment missing. Run bin/ci setup first.')
    env = dict(env)
    env['PATH'] = str(docs_python().parent) + os.pathsep + env['PATH']
    with tempfile.TemporaryDirectory(prefix='docs-', dir=STATE) as temp:
        snapshot = Path(temp)
        for relative in source_files():
            source = ROOT / relative
            target = snapshot / relative
            if source.is_file() or source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
        # Generators and Docusaurus write only into the disposable source snapshot.
        (snapshot / 'website/node_modules').symlink_to(ROOT / 'website/node_modules', target_is_directory=True)
        before = docs_inventory(snapshot)
        for script in ('extract-skills.py', 'generate-skill-docs.py'):
            run([str(docs_python()), f'website/scripts/{script}'], cwd=snapshot, env=env)
        check_docs_parity(before, docs_inventory(snapshot))
        run([str(docs_python()), 'website/scripts/check_doc_links.py'], cwd=snapshot, env=env)
        for script in ('lint:diagrams', 'build:fast'):
            run(['npm', 'run', script], cwd=snapshot / 'website', env=env)


def rust(env: dict[str, str]) -> None:
    env = dict(env)
    # Resolve an installed rustup toolchain before entering the isolated HOME.
    # Only its executable directory is shared, never rustup config or credentials.
    if shutil.which('rustup'):
        result = subprocess.run(['rustup', 'which', 'rustc'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode == 0:
            env['PATH'] = str(Path(result.stdout.strip()).parent) + os.pathsep + env['PATH']
    require_tools(('rustc',), env)
    run(['cargo', 'test', '--locked', '--lib'], cwd=ROOT / 'apps/bootstrap-installer/src-tauri', env=env)


def container_lint(env: dict[str, str]) -> None:
    require_tools(('hadolint', 'shellcheck'), env)
    scripts = sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'docker').rglob('*.sh'))
    actions = [('hadolint', lambda: run(['hadolint', '--config', '.hadolint.yaml', '--failure-threshold', 'warning', 'Dockerfile'], env=env)),
               ('shellcheck', lambda: run(['shellcheck', '--severity=error', *scripts], env=env))]
    if not scripts or not aggregate(actions):
        raise RuntimeError('Container lint failed or found no shell scripts')


@contextmanager
def checkout_lock() -> Iterator[None]:
    # Kernel-owned locks release after crashes, unlike a lock-directory sentinel.
    with (STATE / 'running.lock').open('a+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt
                lock.write(b'0')
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('Another CI invocation is using this checkout.') from error
        yield


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='full', choices=('full', 'setup', 'check', 'list', 'preflight', 'gate', 'nightly', 'nightly-native'))
    parser.add_argument('expected_sha', nargs='?', help='Exact committed SHA required by gate/nightly')
    parser.add_argument('--lane', choices=(*LANES, *OPTIONAL_LANES), action='append', help='Partial check, never full-gate evidence')
    parser.add_argument('--workers', type=int, default=4, help='Python file workers (default 4)')
    parser.add_argument('--node-workers', type=int, default=2)
    args = parser.parse_args()
    if args.workers < 1 or args.node_workers < 1:
        parser.error('Worker counts must be positive')
    if args.command != 'check' and args.lane:
        parser.error('--lane is only valid for check')
    exact = args.command in ('gate', 'nightly', 'nightly-native')
    if exact != bool(args.expected_sha):
        parser.error('gate/nightly require a positional full SHA; other commands do not accept one')
    if args.command == 'preflight':
        preflight()
        return 0
    if exact:
        assert_exact_checkout(args.expected_sha)
    if args.command == 'list':
        for name, description in {**LANES, **OPTIONAL_LANES}.items():
            print(f'{name}: {description}')
        print('Residual platform/integration/release coverage: maintenance/portable-ci.md')
        return 0
    STATE.mkdir(exist_ok=True)
    with checkout_lock(), external_temporary_directory('hermes-ci-home-') as home, source_unchanged():
        env = environment(home)
        if args.command in ('setup', 'full', 'gate', 'nightly', 'nightly-native'):
            setup(env)
            if args.command == 'setup':
                return 0
        # Dependency setup is allowed to write ignored state, not tracked source.
        # Assert immediately before checks and again after successful lanes.
        if exact:
            assert_exact_checkout(args.expected_sha)
        lanes = {
            'static': lambda: static(env),
            'python-gate': lambda: python_tests(env, list(GATE_PYTHON_FILES), args.workers),
            'node-gate': lambda: node_gate(env, args.node_workers),
            'python': lambda: python_tests(env, ['tests'], args.workers),
            'e2e': lambda: python_tests(env, ['tests/e2e'], args.workers),
            'node': lambda: node(env, args.node_workers),
            'docs': lambda: docs(env),
            'rust': lambda: rust(env),
            'container-lint': lambda: container_lint(env),
            'native-os': lambda: native_os(env, args.workers),
        }
        selected = args.lane or (list(GATE_LANES) if args.command == 'gate' else
                                 ['native-os'] if args.command == 'nightly-native' else list(LANES))
        passed = aggregate([(name, lanes[name]) for name in dict.fromkeys(selected)])
        if exact:
            assert_exact_checkout(args.expected_sha)
        scope = ('GATE' if args.command == 'gate' else 'NIGHTLY' if exact else
                 'PARTIAL' if args.lane else f'FULL SOURCE GATE ({sys.platform})')
        print(f'{scope}: {"PASS" if passed else "FAIL"}. Native OS, external integration and release lanes are separate.')
        return 0 if passed else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'CI failed: {exc}', file=sys.stderr)
        sys.exit(1)
