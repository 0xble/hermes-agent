"""Which ready PRs need the pre-merge macOS job, on a real Git repository."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('macos_scope', Path(__file__).resolve().parents[1] / 'macos_scope.py')
assert SPEC is not None and SPEC.loader is not None
macos_scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macos_scope)

RUNS = (
    ('gateway/platforms/macos_keychain.py', 'X = 1\n'),
    ('tools/launchd_helper.py', 'X = 1\n'),
    ('agent/core.py', 'import sys\nIS_MAC = sys.platform == "darwin"\n'),
    ('agent/core.py', 'import platform\nif platform.system() != "Darwin":\n    pass\n'),
    ('agent/core.py', 'import sys\nMAC = sys.platform.startswith("darwin")\n'),
    ('agent/core.py', 'import sys\nPOSIX = sys.platform in ("darwin", "linux")\n'),
    ('tests/test_tray.py', '@pytest.mark.platforms("macos")\ndef test_tray():\n    pass\n'),
    ('.github/workflows/gate.yml', 'name: gate\n'),
    ('uv.lock', 'version = 1\n'),
    ('apps/desktop/package.json', '{}\n'),
)


def git(root: Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


class MacosScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 'ci@example.invalid')
        git(self.repo, 'config', 'user.name', 'ci')
        self.write('agent/core.py', 'X = 1\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'base')
        git(self.repo, 'switch', '-qc', 'change')
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def write(self, path: str, text: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(text, encoding='utf-8')

    def commit(self, path: str, text: str) -> None:
        self.write(path, text)
        git(self.repo, 'add', path)
        git(self.repo, 'commit', '-qm', 'change')

    def test_fork_sync_candidates_always_run(self) -> None:
        self.assertIs(macos_scope.decide('sync/controller-abc-r1', 'main')[0], True)

    def test_macos_touching_changes_run(self) -> None:
        for path, text in RUNS:
            with self.subTest(path=path, text=text):
                git(self.repo, 'switch', '-qC', 'change', 'main')
                self.commit(path, text)
                run, reason = macos_scope.decide('feat/x', 'main')
                self.assertIs(run, True, reason)

    def test_linux_only_change_skips(self) -> None:
        self.commit('agent/core.py', 'X = 2\n')
        self.assertEqual(macos_scope.decide('feat/x', 'main'), (False, 'no macOS-specific change'))

    def test_deleted_python_runs_to_be_safe(self) -> None:
        git(self.repo, 'rm', '-q', 'agent/core.py')
        git(self.repo, 'commit', '-qm', 'delete')
        run, reason = macos_scope.decide('feat/x', 'main')
        self.assertIs(run, True, reason)

    def test_unreadable_diff_runs_to_be_safe(self) -> None:
        run, reason = macos_scope.decide('feat/x', 'no-such-ref')
        self.assertIs(run, True)
        self.assertIn('running to be safe', reason)


if __name__ == '__main__':
    unittest.main()
