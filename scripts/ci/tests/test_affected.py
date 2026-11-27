"""Exercise the PR shard planner against real Git history and inventory."""
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


LEAF = 'tests/gateway/test_response_filters.py'
SOURCE = 'gateway/response_filters.py'
SHARED = 'tests/acp_adapter/test_acp_image_staging_boundary.py'
PLUGIN = 'tests/gateway/test_session_bot_retry.py'
FIXTURE = 'tests/gateway/test_normal_runtime_boot.py'
INFRA = 'tests/scripts/ci/test_portable_windows_environment.py'
CONSUMER = 'tests/acp_adapter/test_gateway_event_mapping.py'
E2E = 'tests/e2e/test_smoke.py'
LEAF_SOURCE = 'def test_leaf():\n    assert 1 == 1\n'


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env = portable.git_environment(root=None)
        self.git('init', '-q', '-b', 'main')
        self.write(LEAF, LEAF_SOURCE)
        self.write(SHARED, 'PNG = b"image"\n')
        self.write(PLUGIN, 'import pytest\n\n@pytest.fixture\ndef bot():\n    return 1\n')
        self.write(FIXTURE, 'def control():\n    return 1\n')
        self.write(INFRA, 'def test_runner():\n    assert True\n')
        self.write(CONSUMER, 'from gateway.run_turn_runner import TurnRunner\n')
        self.write(E2E, 'def test_smoke():\n    assert True\n')
        self.write('tests/conftest.py', 'from tests.gateway.test_normal_runtime_boot import control\n')
        self.write('tests/acp_adapter/test_acp_discard_review.py',
                   'from tests.acp_adapter.test_acp_image_staging_boundary import PNG\n')
        self.write('tests/gateway/test_bot_mailbox_async.py',
                   'pytest_plugins = ("tests.gateway.test_session_bot_retry",)\n')
        for path in (SOURCE, 'uv.lock', 'scripts/ci/python_shard_timings.json'):
            self.write(path, '{}\n')
        self.base = self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.enterContext(patch.object(portable, 'ROOT', self.root))
        self.enterContext(patch.object(portable, 'STATE', self.root / '.ci'))
        self.enterContext(patch.object(affected, 'AUDITED_LEAF_TESTS',
                                       {LEAF: self.git('rev-parse', f'{self.base}:{LEAF}').strip()}))

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, env=self.env,
                                       text=True, encoding='utf-8', stderr=subprocess.PIPE)

    def write(self, path, content):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding='utf-8')

    def commit(self, *, stage=True):
        if stage:
            self.git('add', '--all')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD').strip()

    def assert_full(self, head):
        selection, buckets = portable.ready_plan(head, 3)
        full = portable.shard_files(self.root, 3)
        self.assertEqual((selection.selection, buckets), ('full', full))
        self.assertEqual(selection.tests, tuple(sorted(p for bucket in full for p in bucket)))
        return selection

    def test_audited_body_edit_compares_merge_base_and_runs_once(self):
        self.write('tests/conftest.py', '# main only\n')
        self.git('update-ref', 'refs/remotes/origin/main', self.commit())
        self.git('switch', '--detach', self.base)
        self.write(LEAF, LEAF_SOURCE.replace('1 == 1', '2 == 2'))
        selection, buckets = portable.ready_plan(self.commit(), 3)
        self.assertEqual((selection.base, selection.selection, selection.tests), (self.base, 'affected', (LEAF,)))
        self.assertEqual([p for bucket in buckets for p in bucket], [LEAF])
        for narrow, full in zip(buckets, portable.shard_files(self.root, 3), strict=True):
            self.assertEqual(narrow, [p for p in full if p == LEAF])

    def test_unproven_files_in_the_inventory_do_not_narrow(self):
        for path in (SHARED, PLUGIN, FIXTURE, INFRA, E2E, 'tests/gateway/test_new.py'):
            with self.subTest(path=path):
                self.git('switch', '--detach', self.base)
                file = self.root / path
                self.write(path, (file.read_text(encoding='utf-8') if file.exists() else '') + '\n# edit\n')
                head = self.commit()
                self.assertIn(path, {p for bucket in portable.shard_files(self.root, 3) for p in bucket})
                self.assert_full(head)

    def test_production_and_mixed_inputs_are_full_even_with_an_audited_test(self):
        self.write(SOURCE, '# production only\n')
        self.assertIn(CONSUMER, self.assert_full(self.commit()).tests)
        for path in (SOURCE, SHARED, 'tests/conftest.py', 'uv.lock', '.github/workflows/gate.yml'):
            with self.subTest(path=path):
                self.git('switch', '--detach', self.base)
                self.write(LEAF, LEAF_SOURCE.replace('1 == 1', '2 == 2'))
                self.write(path, '# mixed\n')
                self.assert_full(self.commit())

    def test_audited_file_cannot_change_module_scope_or_test_definition(self):
        for content in (
            'pytest_plugins = ("another_plugin",)\n' + LEAF_SOURCE,
            'import pytest\n\n@pytest.fixture\n' + LEAF_SOURCE,
            LEAF_SOURCE.replace('test_leaf()', 'test_leaf(tmp_path)'),
            LEAF_SOURCE + '\nraise RuntimeError("import-time side effect")\n',
            'def test_leaf(:\n',
            'def test_leaf():\n    break\n',
        ):
            with self.subTest(content=content):
                self.git('switch', '--detach', self.base)
                self.write(LEAF, content)
                self.assert_full(self.commit())
        self.git('switch', '--detach', self.base)
        (self.root / LEAF).write_bytes(b'def test_leaf():\n    # \xff\n    pass\n')
        self.assert_full(self.commit())

    def test_renamed_test_forces_full_selection(self):
        renamed = 'tests/gateway/test_relocated.py'
        self.git('mv', LEAF, renamed)
        self.assertIn(renamed, self.assert_full(self.commit()).tests)

    def test_body_edit_after_main_changes_the_audited_module_contract_is_full(self):
        source = 'pytest_plugins = ("another_plugin",)\n' + LEAF_SOURCE
        self.write(LEAF, source)
        self.git('update-ref', 'refs/remotes/origin/main', self.commit())
        self.write(LEAF, source.replace('1 == 1', '2 == 2'))
        self.assert_full(self.commit())

    def test_file_mode_change_cannot_narrow(self):
        self.git('update-index', '--chmod=+x', LEAF)
        self.assert_full(self.commit(stage=False))

    def test_empty_diff_and_missing_base_are_not_noops(self):
        self.assertEqual(self.assert_full(self.base).base, self.base)
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assertIsNone(self.assert_full(self.base).base)

    def test_shallow_history_forces_full_even_with_a_resolvable_base(self):
        self.write(LEAF, LEAF_SOURCE.replace('1 == 1', '2 == 2'))
        head = self.commit()
        (self.root / '.git/shallow').write_text(self.base + '\n', encoding='utf-8')
        self.assertEqual(self.git('merge-base', 'origin/main', head).strip(), self.base)
        self.assertIsNone(self.assert_full(head).base)

    def test_unreadable_diff_or_blob_uses_canonical_shards(self):
        self.write(LEAF, LEAF_SOURCE.replace('1 == 1', '2 == 2'))
        head = self.commit()
        errors = (
            ('diff', f'M\0{LEAF}'),  # truncated NUL record
            ('diff', UnicodeError('undecodable Git path')),
            ('diff', subprocess.TimeoutExpired('git diff', 1)),
            ('cat-file', subprocess.CalledProcessError(128, 'git cat-file')),
        )
        for command, failure in errors:
            with self.subTest(command=command, failure=failure):
                def git(*args, **kwargs):
                    if args[0] != command:
                        return self.git(*args)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                with patch.object(portable, 'git', side_effect=git):
                    self.assert_full(head)

    def test_ready_shards_dispatch_the_full_buckets_for_a_shared_plugin_edit(self):
        self.write(PLUGIN, (self.root / PLUGIN).read_text(encoding='utf-8').replace('return 1', 'return 2'))
        head = self.commit()
        full = portable.shard_files(self.root, 3)
        with (patch.object(portable, 'ci_environment', return_value=nullcontext({})),
              patch.object(portable, 'setup'), patch.object(portable, 'python_files') as run):
            for index in range(1, 4):
                with patch('sys.argv', ['bin/ci', 'ready', head, '--shard', f'{index}/3']):
                    self.assertEqual(portable.main(), 0)
        self.assertEqual([args.args[1] for args in run.call_args_list], full)

    def test_unsharded_exact_gate_runs_full_shards_not_the_selector(self):
        self.write(LEAF, LEAF_SOURCE.replace('1 == 1', '2 == 2'))
        head = self.commit()
        full = portable.shard_files(self.root, portable.PR_SHARDS)
        with (patch.object(portable, 'ci_environment', return_value=nullcontext({})),
              patch.object(portable, 'setup'), patch.object(portable, 'static') as static,
              patch.object(portable, 'node_gate') as node,
              patch.object(portable, 'python_files') as run,
              patch.object(portable, 'ready_plan', side_effect=AssertionError('gate cannot narrow')),
              patch('sys.argv', ['bin/ci', 'gate', head])):
            self.assertEqual(portable.main(), 0)
        static.assert_called_once()
        node.assert_called_once()
        self.assertEqual([args.args[1] for args in run.call_args_list], [b for b in full if b])


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
