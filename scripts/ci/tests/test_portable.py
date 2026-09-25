"""Focused stdlib tests of the contributor-gate boundaries (no app dependencies)."""
from contextlib import ExitStack, nullcontext
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / 'portable.py'
spec = importlib.util.spec_from_file_location('portable_ci', MODULE_PATH)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def clean_git_env():
    """Keep temporary repositories independent of hook and personal Git state."""
    return ci.git_environment(root=None)


class PortableGateTests(unittest.TestCase):
    def test_container_jobs_install_git_and_configure_safe_directory_before_checkout(self):
        import yaml

        workflows = Path(__file__).resolve().parents[3] / '.github' / 'workflows'
        for workflow_name in ('gate.yml', 'nightly.yml'):
            with self.subTest(workflow=workflow_name):
                document = yaml.safe_load((workflows / workflow_name).read_text(encoding='utf-8'))
                for job_name, job in document['jobs'].items():
                    if 'container' not in job:
                        continue
                    steps = job['steps']
                    checkout_index = next(
                        index for index, step in enumerate(steps)
                        if str(step.get('uses', '')).startswith('actions/checkout@')
                    )
                    preceding = steps[:checkout_index]
                    self.assertTrue(
                        any(
                            'apt-get install' in step.get('run', '')
                            and 'git' in step.get('run', '')
                            and 'ca-certificates' in step.get('run', '')
                            for step in preceding
                        ),
                        f'{workflow_name}:{job_name} must install git and ca-certificates before checkout',
                    )
                    self.assertTrue(
                        any(
                            'git config --global --add safe.directory "$GITHUB_WORKSPACE"' in step.get('run', '')
                            for step in preceding
                        ),
                        f'{workflow_name}:{job_name} must configure Git safe.directory before checkout',
                    )

    def test_nightly_reaps_orphans_and_runs_python_suite_as_nonroot(self):
        import yaml

        workflow = Path(__file__).resolve().parents[3] / '.github/workflows/nightly.yml'
        linux = yaml.safe_load(workflow.read_text(encoding='utf-8'))['jobs']['linux']
        self.assertIn('--init', linux['container']['options'].split())
        install = next(step for step in linux['steps'] if step.get('name') == 'Install pinned Linux toolchain and platform libraries')
        self.assertIn(' ffmpeg ', install['run'])
        profile = next(step for step in linux['steps'] if step.get('name') == 'Broad exact-SHA source profile')
        self.assertIn('runuser -u ci -- env HOME="$HOME" ./bin/ci nightly', profile['run'])
        self.assertIn('chown -R ci:ci "$GITHUB_WORKSPACE" "$HOME"', profile['run'])

    def test_exact_checkout_rejects_malformed_wrong_and_mutated_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = clean_git_env()
            subprocess.run(['git', 'init', '-q', str(root)], env=env, check=True)
            subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'ci@example.invalid'], env=env, check=True)
            subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'CI'], env=env, check=True)
            tracked = root / 'tracked.txt'
            tracked.write_text('original', encoding='utf-8')
            subprocess.run(['git', '-C', str(root), 'add', 'tracked.txt'], env=env, check=True)
            subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'fixture'], env=env, check=True)
            with patch.object(ci, 'ROOT', root):
                sha = ci.git('rev-parse', 'HEAD').strip()
                ci.assert_exact_checkout(sha)
                with self.assertRaisesRegex(RuntimeError, 'full lowercase'):
                    ci.assert_exact_checkout('bad')
                with self.assertRaisesRegex(RuntimeError, 'SHA mismatch'):
                    ci.assert_exact_checkout('0' * 40 if sha != '0' * 40 else '1' * 40)
                tracked.write_text('changed during checks', encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'Tracked checkout'):
                    ci.assert_exact_checkout(sha)
                subprocess.run(['git', '-C', str(root), 'add', 'tracked.txt'], env=env, check=True)
                with self.assertRaisesRegex(RuntimeError, 'Tracked checkout'):
                    ci.assert_exact_checkout(sha)

    def test_preflight_fixture_cannot_mutate_inherited_git_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / 'sentinel'
            sentinel.mkdir()
            env = clean_git_env()
            subprocess.run(['git', 'init', '-q', str(sentinel)], env=env, check=True)
            subprocess.run(['git', '-C', str(sentinel), '-c', 'user.name=Sentinel',
                            '-c', 'user.email=sentinel@example.invalid', 'commit',
                            '--allow-empty', '-qm', 'sentinel'], env=env, check=True)
            git_dir = sentinel / '.git'
            before = {name: (git_dir / name).read_bytes() for name in ('HEAD', 'index', 'config') if (git_dir / name).exists()}
            poisoned = dict(env, GIT_DIR=str(git_dir), GIT_PREFIX='sub/')
            result = subprocess.run(
                [sys.executable, '-m', 'unittest',
                 'scripts.ci.tests.test_portable.PortableGateTests.test_exact_checkout_rejects_malformed_wrong_and_mutated_sha'],
                cwd=ci.ROOT, env=poisoned, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            after = {name: (git_dir / name).read_bytes() for name in ('HEAD', 'index', 'config') if (git_dir / name).exists()}
            self.assertEqual(after, before, 'preflight fixture modified the inherited repository')

    def test_failures_do_not_hide_later_results(self):
        seen = []

        def fail():
            seen.append('failure')
            raise subprocess.CalledProcessError(7, ['broken-check'])

        self.assertFalse(ci.aggregate([('broken', fail), ('next', lambda: seen.append('next'))]))
        self.assertEqual(seen, ['failure', 'next'])

    def test_clean_environment_blocks_credentials_personal_plugins_and_partial_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            poisoned = {
                'PATH': os.defpath,
                'HOME': str(state / 'personal'),
                'OPENAI_API_KEY': 'never-forward',
                'PYTEST_PLUGINS': 'personal_guard',
                'HERMES_TEST_PATHS': 'single-file.py',
                'HERMES_TEST_SLICE': '1/100',
                'NODE_OPTIONS': '--require=personal.js',
                'UV_PROJECT_ENVIRONMENT': '/unrelated/venv',
            }
            with patch.dict(os.environ, poisoned, clear=True), patch.object(ci, 'STATE', state):
                env = ci.environment(state / 'isolated')
            for key in ('OPENAI_API_KEY', 'PYTEST_PLUGINS', 'HERMES_TEST_PATHS', 'HERMES_TEST_SLICE', 'NODE_OPTIONS'):
                self.assertNotIn(key, env)
            self.assertEqual(env['HOME'], str(state / 'isolated'))
            self.assertEqual(env['CARGO_HOME'], str(state / 'cargo'))
            self.assertEqual(env['UV_PROJECT_ENVIRONMENT'], str(ci.ROOT / '.venv'))
            config = Path(env['GIT_CONFIG_GLOBAL'])
            self.assertEqual(config, (state / 'isolated' / 'gitconfig').resolve())
            self.assertEqual(config.read_text(encoding='utf-8'),
                             f'[safe]\n\tdirectory = {ci.ROOT.resolve().as_posix()}\n')
            self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')
            directories = subprocess.check_output(
                ['git', 'config', '--global', '--get-all', 'safe.directory'],
                env=env, text=True,
            ).splitlines()
            self.assertEqual(directories, [ci.ROOT.resolve().as_posix()])

    def test_python_file_runner_preserves_only_isolated_git_config(self):
        # The shell runner clears its environment before spawning pytest. The
        # per-file process still needs the isolated checkout's safe.directory
        # when root runs against a runner-owned GitHub Actions workspace.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_file = root / 'test_runner_git_config.py'
            test_file.write_text(
                'import os, subprocess\n'
                'def test_isolated_checkout_config():\n'
                '    config = os.environ["GIT_CONFIG_GLOBAL"]\n'
                '    assert config.endswith("/gitconfig")\n'
                '    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"\n'
                '    directories = subprocess.check_output(\n'
                '        ["git", "config", "--global", "--get-all", "safe.directory"], text=True\n'
                '    ).splitlines()\n'
                f'    assert directories == [{str(ci.ROOT.resolve())!r}]\n',
                encoding='utf-8',
            )
            env = ci.environment(root / 'isolated')
            env['HERMES_PYTHON'] = sys.executable
            env['HERMES_TEST_SCRATCH_ROOT'] = str(root / 'scratch')
            result = subprocess.run(
                ['bash', 'scripts/run_tests.sh', '-j', '1', '--file-retries', '0', str(test_file)],
                cwd=ci.ROOT, env=env, capture_output=True, text=True,
                encoding='utf-8', errors='replace',
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_external_fixture_has_no_git_or_node_dependency_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout = base / 'checkout'
            checkout.mkdir()
            subprocess.run(['git', 'init', '-q', str(checkout)], env=clean_git_env(), check=True)
            package = checkout / 'node_modules/ci-ancestry-probe'
            package.mkdir(parents=True)
            (package / 'index.js').write_text('module.exports = true', encoding='utf-8')
            contaminated = checkout / '.ci/home/tmp/plain'
            contaminated.mkdir(parents=True)
            script = """
const {createRequire} = require('node:module');
try {
  createRequire(process.cwd() + '/probe.cjs').resolve('ci-ancestry-probe');
  process.exitCode = 5;
} catch (error) {
  if (error.code !== 'MODULE_NOT_FOUND') throw error;
}
"""
            env = clean_git_env()
            old_git = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=contaminated, env=env, capture_output=True)
            old_node = subprocess.run(['node', '-e', script], cwd=contaminated,
                                      env={k: v for k, v in env.items() if not k.startswith('NODE_')}, capture_output=True)
            self.assertEqual(old_git.returncode, 0)
            self.assertEqual(old_node.returncode, 5)
            with ci.external_temporary_directory('home-', parent=base) as home:
                child_env = ci.environment(home)
                fixture = Path(child_env['TMPDIR']) / 'plain'
                fixture.mkdir()
                clean_git = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=fixture, env=child_env, capture_output=True)
                clean_node = subprocess.run(['node', '-e', script], cwd=fixture, env=child_env, capture_output=True)
                self.assertNotEqual(clean_git.returncode, 0)
                self.assertEqual(clean_node.returncode, 0, clean_node.stderr)
            self.assertFalse(home.exists())
            self.assertTrue(package.exists())

    def test_temporary_parent_with_git_or_node_ancestry_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for marker in ('.git', 'node_modules'):
                parent = base / marker.replace('.', 'dot')
                parent.mkdir()
                (parent / marker).mkdir()
                nested = parent / 'temporary'
                nested.mkdir()
                with self.assertRaisesRegex(RuntimeError, 'ancestry'):
                    with ci.external_temporary_directory('home-', parent=nested):
                        self.fail('unsafe ancestry admitted')

    def test_docs_parity_detects_added_deleted_and_changed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / 'website/docs'
            docs.mkdir(parents=True)
            page = docs / 'page.md'
            page.write_text('source', encoding='utf-8')
            before = ci.docs_inventory(root)
            ci.check_docs_parity(before, ci.docs_inventory(root))
            page.write_text('generated change', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'page.md'):
                ci.check_docs_parity(before, ci.docs_inventory(root))
            page.unlink()
            with self.assertRaisesRegex(RuntimeError, 'page.md'):
                ci.check_docs_parity(before, ci.docs_inventory(root))
            page.write_text('source', encoding='utf-8')
            (docs / 'added.md').write_text('new', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'added.md'):
                ci.check_docs_parity(before, ci.docs_inventory(root))

    def test_every_reusable_only_workflow_has_a_caller(self):
        import yaml
        workflows = ci.ROOT / '.github/workflows'
        texts = {path.name: path.read_text(encoding='utf-8') for path in workflows.glob('*.y*ml')}
        for name, text in texts.items():
            triggers = yaml.safe_load(text).get(True) or yaml.safe_load(text).get('on') or {}
            if isinstance(triggers, dict) and set(triggers) <= {'workflow_call', 'workflow_dispatch'} and 'workflow_call' in triggers:
                callers = [other for other, body in texts.items() if other != name and f'./.github/workflows/{name}' in body]
                self.assertTrue(callers, f'{name} is reusable-only and nothing calls it')
        self.assertIn('desktop-core', yaml.safe_load(texts['nightly.yml'])['jobs']['qualification']['needs'])

    def test_every_local_action_reference_resolves(self):
        import re
        missing = []
        for path in sorted((ci.ROOT / '.github/workflows').glob('*.y*ml')):
            for ref in re.findall(r'uses:\s*(\./[^\s#]+)', path.read_text(encoding='utf-8')):
                target = ci.ROOT / ref
                if not (target.is_file() or any((target / name).is_file() for name in ('action.yml', 'action.yaml', 'Dockerfile'))):
                    missing.append(f'{path.name}: {ref}')
        self.assertEqual(missing, [])

    def test_source_guard_detects_mutation_without_overwriting_user_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], env=clean_git_env(), check=True)
            source = root / 'source.txt'
            source.write_text('uncommitted user work', encoding='utf-8')
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'source_files', return_value=['source.txt']):
                with ci.source_unchanged():
                    pass
                with self.assertRaisesRegex(RuntimeError, 'source.txt'):
                    with ci.source_unchanged():
                        source.write_text('unexpected generated output', encoding='utf-8')
            self.assertEqual(source.read_text(encoding='utf-8'), 'unexpected generated output')

    def test_source_guard_detects_source_created_during_ci(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], env=clean_git_env(), check=True)
            (root / 'existing.py').write_text('x = 1\n', encoding='utf-8')
            with patch.object(ci, 'ROOT', root):
                with self.assertRaisesRegex(RuntimeError, 'new_source.py'):
                    with ci.source_unchanged():
                        (root / 'new_source.py').write_text('y = 2\n', encoding='utf-8')

    def test_tool_version_handles_node_v_prefix_and_rejects_wrong_pin(self):
        with patch.object(ci.subprocess, 'check_output', return_value='v' + ci.PINS['node'] + '\n'):
            ci.require_tools(('node',), {})
        with patch.object(ci.subprocess, 'check_output', return_value='v0.0.0\n'):
            with self.assertRaisesRegex(RuntimeError, 'require'):
                ci.require_tools(('node',), {})

    def test_setup_provisions_pinned_npm_and_rg_ahead_of_host_tools(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            toolchain = Path(directory) / 'toolchain'
            stack.enter_context(patch.object(ci, 'TOOLCHAIN', toolchain))
            commands = []

            def install(argv, **kwargs):
                commands.append(argv)
                package = toolchain / 'node_modules' / 'npm' / 'package.json'
                package.parent.mkdir(parents=True, exist_ok=True)
                package.write_text('{"version": "%s"}' % ci.PINS['npm'], encoding='utf-8')

            stack.enter_context(patch.object(ci, 'run', side_effect=install))
            ci.provision_npm({})
            ci.provision_npm({})
            self.assertEqual(len(commands), 1)
            self.assertIn(f"npm@{ci.PINS['npm']}", commands[0])
            self.assertEqual(commands[0][commands[0].index('--prefix') + 1], str(toolchain))
            with patch.dict(os.environ, {'PATH': '/host/bin'}, clear=True), patch.object(ci, 'STATE', Path(directory)):
                path = ci.environment(Path(directory) / 'home')['PATH'].split(os.pathsep)
            self.assertLess(path.index(str(toolchain / 'node_modules' / '.bin')), path.index('/host/bin'))
            self.assertLess(path.index(str(toolchain / 'bin')), path.index('/host/bin'))
            commands.clear()
            stack.enter_context(patch.object(ci, 'rust_environment', side_effect=lambda env: env))
            versions = iter(('ripgrep 99.0.0\n',))
            stack.enter_context(patch.object(ci.subprocess, 'check_output', side_effect=lambda *a, **k: next(versions)))
            ci.provision_rg({})
            self.assertEqual(commands[-1][:2], ['cargo', 'install'])
            self.assertIn(f"ripgrep@{ci.PINS['rg']}", commands[-1])

    def test_default_invocation_sets_up_and_runs_all_lanes_after_failure(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(ci, 'STATE', Path(directory)))
            stack.enter_context(patch.object(ci, 'source_unchanged', return_value=nullcontext()))
            stack.enter_context(patch.object(sys, 'argv', ['bin/ci']))
            seen = []
            stack.enter_context(patch.object(ci, 'setup', side_effect=lambda env: seen.append('setup')))
            for name in ('static', 'node', 'docs', 'rust', 'container_lint'):
                def action(*args, name=name):
                    seen.append(name)
                    if name == 'static':
                        raise RuntimeError('intentional failure')
                stack.enter_context(patch.object(ci, name, side_effect=action))
            stack.enter_context(patch.object(ci, 'python_tests', side_effect=lambda env, roots, workers: seen.append(tuple(roots))))
            self.assertEqual(ci.main(), 1)
            self.assertEqual(seen, ['setup', 'static', ('tests',), ('tests/e2e',), 'node', 'docs', 'rust', 'container_lint'])

    def test_python_gate_preserves_first_failure_but_interactive_runner_can_retry(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            attempts = root / 'attempts'
            test_file = root / 'test_fail_once.py'
            test_file.write_text(
                'from pathlib import Path\n'
                'def test_fail_once():\n'
                f'    attempts = Path({str(attempts)!r})\n'
                '    count = int(attempts.read_text(encoding="utf-8")) + 1 if attempts.exists() else 1\n'
                '    attempts.write_text(str(count), encoding="utf-8")\n'
                '    assert count > 1, "first attempt fails"\n', encoding='utf-8')
            stack.enter_context(patch.object(ci, 'STATE', root / 'state'))
            stack.enter_context(patch.object(ci, 'source_unchanged', return_value=nullcontext()))
            stack.enter_context(patch.object(sys, 'argv', ['bin/ci', 'check', '--lane', 'python', '--workers', '1']))
            # Keep the actual lane, shell harness and pytest subprocesses. Only
            # narrow discovery and bypass unrelated installed-tool qualification.
            stack.enter_context(patch.object(ci, 'python', return_value=sys.executable))
            stack.enter_context(patch.object(ci, 'require_tools'))
            python_tests = ci.python_tests
            stack.enter_context(patch.object(ci, 'python_tests', side_effect=
                lambda env, roots, workers: python_tests(env, [str(test_file)], workers)))
            self.assertEqual(ci.main(), 1)
            self.assertEqual(attempts.read_text(encoding='utf-8'), '1')

            attempts.unlink()
            interactive_env = ci.environment(root / 'interactive')
            # The direct shell runner also needs writable scratch when the
            # sandbox's default disk-backed temporary directory is read-only.
            interactive_env['HERMES_TEST_SCRATCH_ROOT'] = str(root / 'interactive-scratch')
            interactive_env['HERMES_PYTHON'] = sys.executable
            result = subprocess.run(['bash', 'scripts/run_tests.sh', '-j', '1', str(test_file)],
                                    cwd=ci.ROOT, env=interactive_env,
                                    capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(attempts.read_text(encoding='utf-8'), '2')

    def test_checkout_lock_releases_when_owner_is_killed(self):
        with tempfile.TemporaryDirectory() as directory:
            code = """
import importlib.util, pathlib, sys, time
spec = importlib.util.spec_from_file_location('ci', sys.argv[1])
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
ci.STATE = pathlib.Path(sys.argv[2])
with ci.checkout_lock():
    print('locked', flush=True)
    time.sleep(30)
"""
            owner = subprocess.Popen([sys.executable, '-c', code, str(MODULE_PATH), directory], stdout=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'locked')
                with patch.object(ci, 'STATE', Path(directory)):
                    with self.assertRaisesRegex(RuntimeError, 'Another CI'):
                        with ci.checkout_lock():
                            self.fail('concurrent owner admitted')
                    owner.kill()
                    owner.wait(timeout=5)
                    with ci.checkout_lock():
                        pass
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
                owner.stdout.close()

    def test_entrypoint_is_independent_of_cwd_and_rejects_bad_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, str(ci.ROOT / 'bin/ci')]
            result = subprocess.run([*command, 'list'], cwd=directory, capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('python', result.stdout)
            bad = subprocess.run([*command, 'check', '--workers', '0'], cwd=directory, capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('positive', bad.stderr)


if __name__ == '__main__':
    unittest.main()
