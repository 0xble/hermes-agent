"""The portable static gate must enforce code health against the PR merge-base."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'portable_health_ci', Path(__file__).resolve().parents[1] / 'portable.py',
)
assert spec is not None and spec.loader is not None
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class CodeHealthGateTests(unittest.TestCase):
    def run_static(self, *, reject_health=False):
        commands = []

        def run(argv, **kwargs):
            commands.append(argv)
            if reject_health and 'scripts.code_health' in argv:
                raise subprocess.CalledProcessError(1, argv)

        with (
            patch.object(ci, 'require_tools'),
            patch.object(ci, 'python', return_value='checkout-python'),
            patch.object(ci, 'git', return_value='merge-base-sha\n') as git,
            patch.object(ci, 'history_policy'),
            patch.object(ci, 'run', side_effect=run),
        ):
            if reject_health:
                with self.assertRaisesRegex(RuntimeError, 'Blocking static checks failed'):
                    ci.static({})
            else:
                ci.static({})
        git.assert_called_once_with('merge-base', 'origin/main', 'HEAD')
        self.assertIn(
            ['checkout-python', '-m', 'scripts.code_health', '--base', 'merge-base-sha',
             '--head', 'HEAD'], commands,
        )

    def test_static_ratchets_exact_head_against_merge_base(self):
        self.run_static()

    def test_new_blocking_health_finding_fails_static(self):
        self.run_static(reject_health=True)
