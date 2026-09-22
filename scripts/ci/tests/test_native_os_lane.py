"""Native-lane dispatch contracts, not evidence of actual macOS/Windows tests."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'portable.py'
spec = importlib.util.spec_from_file_location('portable_ci', PATH)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class NativeOsLaneTests(unittest.TestCase):
    def test_unsupported_host_rejected_before_any_selection_or_test(self):
        with patch.object(ci.sys, 'platform', 'linux'), patch.object(ci.subprocess, 'check_output') as selector, patch.object(ci, 'python_tests') as tests:
            with self.assertRaisesRegex(RuntimeError, 'actual macOS or Windows'):
                ci.native_os({}, 4)
            selector.assert_not_called()
            tests.assert_not_called()

    def test_macos_dispatch_preserves_selected_files_and_marker(self):
        env = {'HOME': '/isolated-home'}
        with patch.object(ci.sys, 'platform', 'darwin'), patch.object(ci, 'python', return_value='checkout-python'), patch.object(ci.subprocess, 'check_output', return_value='tests/a.py\ntests/b.py\n') as selector, patch.object(ci, 'python_tests') as tests, patch.object(ci, 'run') as installer:
            ci.native_os(env, 3)
            self.assertEqual(selector.call_args.args[0], ['checkout-python', 'scripts/ci/list_os_marked_tests.py', 'macos_only'])
            self.assertEqual(selector.call_args.kwargs['env'], env)
            tests.assert_called_once_with(env, ['tests/a.py', 'tests/b.py'], 3, pytest_args=['-m', 'macos_only and not integration'])
            installer.assert_not_called()

    def test_empty_or_failed_selector_never_dispatches_tests(self):
        for output in ('\n ', subprocess.CalledProcessError(1, ['selector'])):
            with self.subTest(output=output), patch.object(ci.sys, 'platform', 'win32'), patch.object(ci, 'python', return_value='checkout-python'), patch.object(ci.subprocess, 'check_output', side_effect=output if isinstance(output, Exception) else None, return_value=output), patch.object(ci, 'python_tests') as tests, patch.object(ci, 'run') as installer:
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    ci.native_os({}, 4)
                tests.assert_not_called()
                installer.assert_not_called()

    def test_windows_runs_all_six_installer_checks_even_after_python_failure(self):
        env = {'HOME': 'isolated-home'}
        with patch.object(ci.sys, 'platform', 'win32'), patch.object(ci, 'python', return_value='checkout-python'), patch.object(ci.subprocess, 'check_output', return_value='tests/windows.py\n'), patch.object(ci, 'python_tests', side_effect=RuntimeError('test failure')) as tests, patch.object(ci, 'run') as installer:
            with self.assertRaisesRegex(RuntimeError, 'Native OS checks failed'):
                ci.native_os(env, 2)
            tests.assert_called_once_with(env, ['tests/windows.py'], 2, pytest_args=['-m', 'windows_only and not integration'])
            calls = [call.args[0] for call in installer.call_args_list]
            self.assertEqual(len(calls), 6)
            for shell in ('powershell', 'pwsh'):
                self.assertEqual({command[-1] for command in calls if command[0] == shell}, {
                    'scripts/tests/test-install-ps1-longpath.ps1',
                    'scripts/tests/test-install-ps1-node-compatibility.ps1',
                    'scripts/tests/test-install-ps1-uv-shim-validation.ps1',
                })
            for call in installer.call_args_list:
                self.assertEqual(call.args[0][1:5], ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File'])
                self.assertEqual(call.kwargs['env'], env)


if __name__ == '__main__':
    unittest.main()
