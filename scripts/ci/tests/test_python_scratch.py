"""Exercise scratch selection through the real canonical shell/parallel runner."""
from contextlib import redirect_stderr
import errno
import io
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('parallel_runner', ROOT / 'scripts/run_tests_parallel.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PythonScratchTests(unittest.TestCase):
    def test_portable_invocation_uses_owned_original_home_when_linux_default_unwritable(self):
        spec = importlib.util.spec_from_file_location('portable_ci', ROOT / 'scripts/ci/portable.py')
        ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ci)
        with tempfile.TemporaryDirectory() as directory:
            original_home = Path(directory)
            sentinel = original_home / 'preserve'
            sentinel.write_text('unrelated', encoding='utf-8')
            for writable in (False, True):
                original_env = {'PATH': os.defpath, 'HOME': str(original_home / 'isolated-home')}
                observed = []

                def observe(command, *, env):
                    self.assertEqual(command, ['bash', 'scripts/run_tests.sh', '-j', '4', '--file-retries', '0', 'tests'])
                    if writable:
                        self.assertNotIn('HERMES_TEST_SCRATCH_ROOT', env)
                    else:
                        scratch = Path(env['HERMES_TEST_SCRATCH_ROOT'])
                        self.assertTrue(scratch.is_dir())
                        self.assertEqual(scratch.parent, original_home.resolve())
                        self.assertFalse(scratch.is_relative_to(ROOT))
                        self.assertFalse(scratch.is_relative_to(Path(env['HOME'])))
                        observed.append(scratch)

                with self.subTest(writable=writable), patch.object(ci, 'python'), patch.object(ci, 'require_wal_capable_sqlite'), patch.object(ci, 'require_tools'), patch.object(ci.sys, 'platform', 'linux'), patch.object(ci.os, 'access', return_value=writable), patch.object(ci.Path, 'home', return_value=original_home), patch.object(ci, 'run', side_effect=observe):
                    ci.python_tests(original_env, ['tests'], 4)
                    self.assertNotIn('HERMES_TEST_SCRATCH_ROOT', original_env)
                for scratch in observed:
                    self.assertFalse(scratch.exists())
                self.assertEqual(sentinel.read_text(encoding='utf-8'), 'unrelated')

    def test_main_rejects_invalid_scratch_once_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / 'ordinary-file'
            invalid.write_text('not a directory', encoding='utf-8')
            errors = io.StringIO()
            with patch.dict(os.environ, {'HERMES_TEST_SCRATCH_ROOT': str(invalid)}), patch.object(sys, 'argv', ['run_tests_parallel.py', '--files', 'tests/one.py:tests/two.py']), patch.object(runner, 'ThreadPoolExecutor') as workers, patch.object(runner.subprocess, 'Popen') as dispatch, redirect_stderr(errors):
                self.assertEqual(runner.main(), 1)
                workers.assert_not_called()
                dispatch.assert_not_called()
            self.assertEqual(errors.getvalue().count('test scratch setup failed'), 1)
            self.assertIn(str(invalid), errors.getvalue())

    def test_main_probes_writes_inside_existing_scratch_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = io.StringIO()
            with patch.dict(os.environ, {'HERMES_TEST_SCRATCH_ROOT': directory}), patch.object(sys, 'argv', ['run_tests_parallel.py', '--files', 'tests/one.py:tests/two.py']), patch.object(runner.tempfile, 'mkdtemp', side_effect=OSError(errno.EROFS, 'Read-only file system')), patch.object(runner, 'ThreadPoolExecutor') as workers, patch.object(runner.subprocess, 'Popen') as dispatch, redirect_stderr(errors):
                self.assertEqual(runner.main(), 1)
                workers.assert_not_called()
                dispatch.assert_not_called()
            self.assertEqual(errors.getvalue().count('test scratch setup failed'), 1)
            self.assertIn('Read-only file system', errors.getvalue())

    def test_relative_explicit_scratch_is_rejected(self):
        with patch.dict(os.environ, {'HERMES_TEST_SCRATCH_ROOT': 'relative'}):
            with self.assertRaisesRegex(ValueError, 'absolute path'):
                runner._runner_scratch_root()

    def test_explicit_scratch_avoids_readonly_default(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / 'disk-fixtures'
            makedirs = os.makedirs

            def readonly_default(path, *args, **kwargs):
                if Path(path) == Path('/var/tmp/hermes-pytest'):
                    raise OSError(errno.EROFS, 'Read-only file system', path)
                return makedirs(path, *args, **kwargs)

            with patch.dict(os.environ, {'HERMES_TEST_SCRATCH_ROOT': str(scratch)}), patch.object(runner.os, 'makedirs', side_effect=readonly_default):
                self.assertEqual(runner._runner_scratch_root(), str(scratch))
            self.assertTrue(scratch.is_dir())

    def test_shell_forwards_scratch_and_parallel_runner_cleans_each_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            for name in ('run_tests.sh', 'run_tests_parallel.py'):
                shutil.copy2(ROOT / 'scripts' / name, root / 'scripts' / name)
            # A bare executable symlink loses relocatable Python's stdlib path
            # on macOS. Use a real, dependency-free venv for the shell boundary.
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv')], check=True)
            (root / 'tests').mkdir()
            (root / 'tests/test_probe.py').write_text('def test_probe(): pass\n', encoding='utf-8')
            scratch = root / 'pytest-tmp'
            # A stand-in pytest module observes the actual per-file subprocess
            # environment. This tests runner plumbing, not application tests.
            (root / 'pytest.py').write_text('''
if __name__ == '__main__':
    import os
    from pathlib import Path
    root = Path(os.environ['HERMES_TEST_SCRATCH_ROOT'])
    temporary = Path(os.environ['TMPDIR'])
    assert temporary.parent == root, (temporary, root)
    assert os.environ['PYTEST_DEBUG_TEMPROOT'] == str(temporary)
    assert temporary.is_dir()
    (temporary / 'fixture').write_text('created', encoding='utf-8')
    assert 'OPENAI_API_KEY' not in os.environ
    print('1 passed in 0.01s')
''', encoding='utf-8')
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            env = dict(os.environ, HOME=str(root / 'home'), HERMES_TEST_SCRATCH_ROOT=str(scratch), OPENAI_API_KEY='must-not-forward')
            result = subprocess.run(['bash', 'scripts/run_tests.sh', '-j', '1', 'tests'], cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('1 tests passed', result.stdout)
            self.assertTrue(scratch.is_dir())
            self.assertEqual(list(scratch.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
