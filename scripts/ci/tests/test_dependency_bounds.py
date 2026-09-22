"""Check added dependency bounds through real Git histories and the checker CLI."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CHECKER = Path(__file__).resolve().parents[1] / 'check_dependency_bounds.py'


class DependencyBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '--initial-branch=main')
        self.git('config', 'user.name', 'CI Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.manifest = self.repo / 'pyproject.toml'
        self.manifest.write_text('[project]\ndependencies = [\n  "existing>=1.0",\n]\n', encoding='utf-8')
        self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    def git(self, *args):
        return subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={os.devnull}', *args], cwd=self.repo, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def check(self, repo=None, *arguments):
        return subprocess.run([sys.executable, str(CHECKER), '--repo', str(repo or self.repo), *arguments], capture_output=True, text=True, encoding='utf-8', errors='replace')

    def add_spec(self, spec):
        content = self.manifest.read_text(encoding='utf-8')
        self.manifest.write_text(content.replace('\n]\n', f'\n  "{spec}",\n]\n'), encoding='utf-8')
        self.commit()

    def test_new_unbounded_dependency_fails_including_extras(self):
        self.add_spec('new_sdk[http]>=2.3')
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('new_sdk[http]>=2.3', result.stderr)
        self.assertNotIn('existing>=1.0', result.stderr)

    def test_dirty_dependency_fails_but_explicit_head_checks_committed_snapshot(self):
        old_head = self.git('rev-parse', 'HEAD')
        content = self.manifest.read_text(encoding='utf-8')
        self.manifest.write_text(content.replace('\n]\n', '\n  "dirty_sdk>=4.2",\n]\n'), encoding='utf-8')
        for staged in (False, True):
            if staged:
                self.git('add', 'pyproject.toml')
            with self.subTest(staged=staged):
                current = self.check()
                self.assertEqual(current.returncode, 1, current.stderr)
                self.assertIn('dirty_sdk>=4.2', current.stderr)
                committed = self.check(None, '--head', old_head)
                self.assertEqual(committed.returncode, 0, committed.stderr)

    def test_working_tree_compares_merge_base_not_advanced_base_tip(self):
        self.git('checkout', '-b', 'base-advanced')
        content = self.manifest.read_text(encoding='utf-8')
        self.manifest.write_text(content.replace('existing>=1.0', 'existing>=1.0,<2'), encoding='utf-8')
        self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('checkout', 'main')
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_upper_bound_pin_and_git_reference_pass(self):
        for spec in ('bounded>=2.0,<3', 'pinned==1.2.3', 'plugin @ git+https://example.invalid/plugin.git@abcdef'):
            self.add_spec(spec)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unchanged_removed_and_other_file_specs_do_not_expand_scope(self):
        self.assertEqual(self.check().returncode, 0)
        self.manifest.write_text('[project]\ndependencies = []\n', encoding='utf-8')
        (self.repo / 'other.toml').write_text('dependency = "other>=3"\n', encoding='utf-8')
        self.commit()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_base_fails_instead_of_treating_empty_diff_as_success(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('history', result.stderr.lower())

    def test_shallow_history_fails_even_when_manifest_does_not_change(self):
        (self.repo / 'other.txt').write_text('new commit', encoding='utf-8')
        self.commit()
        shallow = Path(self.temporary.name) / 'shallow'
        self.git('clone', '--quiet', '--depth=1', self.repo.as_uri(), str(shallow))
        result = self.check(shallow)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('full history', result.stderr.lower())

    def test_unrelated_shallow_ref_does_not_reject_complete_selected_history(self):
        self.git('checkout', '--orphan', 'unrelated')
        (self.repo / 'unrelated.txt').write_text('unrelated root', encoding='utf-8')
        self.commit()
        boundary = self.git('rev-parse', 'HEAD')
        self.git('checkout', 'main')
        self.git('update-ref', 'refs/remotes/unrelated/main', boundary)
        shallow = self.repo / self.git('rev-parse', '--git-path', 'shallow')
        shallow.write_text(boundary + '\n', encoding='utf-8')
        self.assertEqual(self.git('rev-parse', '--is-shallow-repository'), 'true')
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = subprocess.run([sys.executable, str(CHECKER), '--repo', str(self.repo), '--base', 'refs/remotes/unrelated/main'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertNotEqual(selected.returncode, 0)
        self.assertIn('Full history', selected.stderr)

    def test_disconnected_history_fails(self):
        self.git('checkout', '--orphan', 'disconnected')
        (self.repo / 'orphan.txt').write_text('distinct root commit', encoding='utf-8')
        self.commit()
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('history', result.stderr.lower())


if __name__ == '__main__':
    unittest.main()
