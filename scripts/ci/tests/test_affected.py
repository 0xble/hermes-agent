"""Exercise the affected-ready policy against real Git history."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci import affected, portable


class SelectionTests(unittest.TestCase):
    files = ('tests/agent/test_other.py', 'tests/e2e/test_smoke.py',
             'tests/gateway/test_response_filters.py', 'tests/pm/test_packaging.py')

    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(root)
        self.env = portable.git_environment(root=None)
        self.git('init', '-q', '-b', 'main')
        for path in (*self.files, affected.SILENCE_SOURCE, 'tests/conftest.py', 'uv.lock'):
            self.write(path)
        self.base = self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', self.base)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, env=self.env,
                                       text=True, encoding='utf-8', stderr=subprocess.PIPE)

    def write(self, path, content='base\n'):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding='utf-8')

    def commit(self):
        self.git('add', '--all')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD').strip()

    def test_compares_merge_base_not_advanced_main_tree(self):
        self.write('tests/conftest.py', 'main only\n')
        self.git('update-ref', 'refs/remotes/origin/main', self.commit())
        self.git('switch', '--detach', self.base)
        self.write(self.files[0], 'branch only\n')
        result = affected.select(self.git, self.commit(), list(self.files))
        self.assertEqual((result.base, result.selection, result.tests), (self.base, 'affected', self.files[:1]))

    def test_audited_production_change_keeps_consumers_and_e2e(self):
        self.write(affected.SILENCE_SOURCE, 'branch only\n')
        self.write(self.files[2], 'regression\n')
        result = affected.select(self.git, self.commit(), list(self.files))
        self.assertEqual((result.selection, result.tests), ('affected', self.files[:3]))

    def test_mixed_and_shared_inputs_force_full_selection(self):
        for path in ('tests/conftest.py', 'uv.lock', '.github/workflows/gate.yml', 'unmapped.py', self.files[-1]):
            with self.subTest(path=path):
                self.git('switch', '--detach', self.base)
                self.write(affected.SILENCE_SOURCE, 'branch only\n')
                self.write(path, 'branch only\n')
                result = affected.select(self.git, self.commit(), list(self.files))
                self.assertEqual((result.base, result.selection, result.tests), (self.base, 'full', self.files))

    def test_renamed_test_forces_full_selection(self):
        renamed = 'tests/agent/test_new.py'
        self.git('mv', self.files[0], renamed)
        inventory = [*self.files[1:], renamed]
        result = affected.select(self.git, self.commit(), inventory)
        self.assertEqual((result.selection, result.tests), ('full', tuple(sorted(inventory))))

    def test_empty_diff_and_missing_base_are_not_noops(self):
        empty = affected.select(self.git, self.base, list(self.files))
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        missing = affected.select(self.git, self.base, list(self.files))
        self.assertEqual((empty.base, empty.selection, empty.tests), (self.base, 'full', self.files))
        self.assertEqual((missing.base, missing.selection, missing.tests), (None, 'full', self.files))

    def test_shallow_history_forces_full_even_with_a_resolvable_base(self):
        self.write(self.files[0], 'branch only\n')
        head = self.commit()
        (self.root / '.git/shallow').write_text(self.base + '\n', encoding='utf-8')
        self.assertEqual(self.git('merge-base', 'origin/main', head).strip(), self.base)
        result = affected.select(self.git, head, list(self.files))
        self.assertEqual((result.base, result.selection, result.tests), (None, 'full', self.files))


class ReadyTests(unittest.TestCase):
    def test_manifest_requires_all_lanes_and_does_not_replace_existing_file(self):
        sha = 'a' * 40
        args = argparse.Namespace(expected_sha=sha, workers=4, node_workers=2)
        tests = ('tests/agent/test_example.py', 'tests/e2e/test_smoke.py')
        selection = affected.Selection('b' * 40, 'full', tests, 'unknown path')
        cases = [(FileNotFoundError('failed runner'), 1), (None, 0)]
        for failure, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as scratch:
                path = Path(scratch) / 'manifest.json'
                with (patch.dict(os.environ, {'CI_AFFECTED_MANIFEST': str(path)}),
                      patch.object(portable, 'ci_environment', return_value=nullcontext({})),
                      patch.object(portable, 'assert_exact_checkout') as exact,
                      patch.object(portable, 'setup'), patch.object(portable, 'static') as static,
                      patch.object(portable, 'node_gate') as node,
                      patch.object(portable, 'ready_plan', return_value=(selection, [[p] for p in tests])),
                      patch.object(portable, 'python_files', side_effect=[failure, None]) as run):
                    self.assertEqual(portable.run_ready(args, 0, 0), code)
                    self.assertEqual(exact.call_count, 3)
                    self.assertTrue(static.called and node.called)
                    self.assertEqual([attempt.args[1][0] for attempt in run.call_args_list], list(tests))
                    if failure:
                        self.assertFalse(path.exists())
                    else:
                        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {
                            'profile': 'affected', 'sha': sha, 'base': 'b' * 40,
                            'selection': 'full', 'tests': list(tests), 'reason': 'unknown path',
                        })
                        with self.assertRaisesRegex(RuntimeError, 'nonexistent absolute path'):
                            portable.run_ready(args, 0, 0)
