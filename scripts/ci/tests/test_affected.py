"""Dependency-based test selection for the quick lane, on a real Git repository."""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SELECTOR = Path(__file__).resolve().parents[1] / 'affected.py'
SPEC = importlib.util.spec_from_file_location('affected', SELECTOR)
assert SPEC is not None and SPEC.loader is not None
affected = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(affected)

FILES = {
    'pyproject.toml': '[tool.pytest.ini_options]\ntestpaths = ["tests", "plugins"]\n',
    'pkg/__init__.py': '',
    'pkg/core.py': 'VALUE = 1\n',
    'pkg/mid.py': 'from pkg.core import VALUE\n',
    'pkg/lazy.py': 'def later():\n    from pkg import core\n    return core\n',
    'pkg/typed.py': 'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg import core\n',
    'pkg/runtime.py': 'from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n    from pkg import core\n',
    'pkg/other.py': 'X = 2\n',
    'data/settings_fixture.json': '{}\n',
    'tests/conftest.py': '',
    'tests/test_core.py': 'from pkg import core\n',
    'tests/test_mid.py': 'import pkg.mid\n',
    'tests/test_lazy.py': 'import pkg.lazy\n',
    'tests/test_typed.py': 'import pkg.typed\n',
    'tests/test_runtime.py': 'import pkg.runtime\n',
    'tests/test_inline.py': 'def test_it():\n    from pkg.other import X\n',
    'tests/test_reads_fixture.py': 'PATH = "data/settings_fixture.json"\n',
    'tests/test_unrelated.py': 'def test_ok():\n    pass\n',
    'tests/sub/conftest.py': 'from pkg import other\n',
    'tests/sub/test_under_conftest.py': '',
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                          text=True, encoding='utf-8').stdout


class SelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 'ci@example.invalid')
        git(self.repo, 'config', 'user.name', 'ci')
        for path, text in FILES.items():
            self.write(path, text)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'base')
        git(self.repo, 'switch', '-qc', 'change')

    def write(self, path: str, text: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(text, encoding='utf-8')

    def on_base(self, files: dict[str, str]) -> None:
        """Add files on main, then branch the change from there."""
        git(self.repo, 'switch', '-q', 'main')
        for path, text in files.items():
            self.write(path, text)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'base addition')
        git(self.repo, 'switch', '-qC', 'change')

    def change(self, path: str, text: str | None) -> tuple[str, list[str], str]:
        if text is None:
            git(self.repo, 'rm', '-q', path)
        else:
            self.write(path, text)
            git(self.repo, 'add', path)
        git(self.repo, 'commit', '-qm', 'change')
        return affected.choose(self.repo, 'main')

    def test_selects_direct_and_transitive_importers(self) -> None:
        mode, tests, _ = self.change('pkg/core.py', 'VALUE = 3\n')
        self.assertEqual(mode, 'affected')
        self.assertEqual(tests, ['tests/test_core.py', 'tests/test_mid.py', 'tests/test_runtime.py'])

    def test_skips_deferred_and_annotation_only_imports(self) -> None:
        _, tests, _ = self.change('pkg/core.py', 'VALUE = 3\n')
        self.assertNotIn('tests/test_lazy.py', tests)
        self.assertNotIn('tests/test_typed.py', tests)

    def test_follows_imports_inside_test_bodies_and_conftest_scope(self) -> None:
        _, tests, _ = self.change('pkg/other.py', 'X = 3\n')
        self.assertEqual(tests, ['tests/sub/test_under_conftest.py', 'tests/test_inline.py'])

    def test_package_initializer_change_selects_submodule_importers(self) -> None:
        _, tests, _ = self.change('pkg/__init__.py', 'FLAG = True\n')
        self.assertIn('tests/test_mid.py', tests)

    def test_selects_tests_that_name_a_changed_data_file(self) -> None:
        _, tests, _ = self.change('data/settings_fixture.json', '{"a": 1}\n')
        self.assertEqual(tests, ['tests/test_reads_fixture.py'])

    def test_changed_test_runs_itself(self) -> None:
        _, tests, _ = self.change('tests/test_unrelated.py', 'def test_ok():\n    assert True\n')
        self.assertEqual(tests, ['tests/test_unrelated.py'])

    def test_deleted_module_selects_its_importers(self) -> None:
        _, tests, _ = self.change('pkg/mid.py', None)
        self.assertEqual(tests, ['tests/test_mid.py'])

    def test_conftest_fixture_imports_count_even_when_deferred(self) -> None:
        self.on_base({'tests/conftest.py': 'def fixture():\n    from pkg import core\n'})
        _, tests, _ = self.change('pkg/core.py', 'VALUE = 3\n')
        self.assertIn('tests/test_unrelated.py', tests)

    def test_pytest_plugins_constant_and_importorskip_imports_count(self) -> None:
        self.on_base({
            'tests/test_plugin_user.py': 'pytest_plugins = ["pkg.other"]\n',
            'tests/test_dynamic.py': 'import importlib\nTARGET = "pkg.other"\n'
                                     'def test_x():\n    importlib.import_module(TARGET)\n',
            'tests/test_optional.py': 'import pytest\nother = pytest.importorskip("pkg.other")\n',
        })
        _, tests, _ = self.change('pkg/other.py', 'X = 4\n')
        self.assertLessEqual({'tests/test_plugin_user.py', 'tests/test_dynamic.py', 'tests/test_optional.py'},
                             set(tests))

    def test_shared_setup_runs_everything(self) -> None:
        for path in ('tests/conftest.py', 'pyproject.toml', 'uv.lock', '.github/workflows/gate.yml',
                     'rust/tool/Cargo.toml', 'tools/web/package.json', 'typescript/bun.lockb',
                     'pnpm-workspace.yaml', 'sub/poetry.lock'):
            with self.subTest(path=path):
                git(self.repo, 'switch', '-qC', 'change', 'main')
                mode, tests, reason = self.change(path, '# changed\n')
                self.assertEqual((mode, tests), ('full', []))
                self.assertIn(path, reason)

    def test_file_selects_tests_in_its_own_directory_tree(self) -> None:
        self.on_base({'plugins/guard/core.py': 'X = 1\n',
                      'plugins/guard/tests/test_guard.py': 'SPEC = "loads core by path"\n'})
        _, tests, _ = self.change('plugins/guard/core.py', 'X = 2\n')
        self.assertEqual(tests, ['plugins/guard/tests/test_guard.py'])

    def test_prose_beside_ci_configuration_does_not_run_everything(self) -> None:
        mode, tests, _ = self.change('.github/workflows/README.md', '# notes\n')
        self.assertEqual((mode, tests), ('affected', []))

    def test_missing_base_runs_everything(self) -> None:
        mode, _, reason = affected.choose(self.repo, 'no-such-ref')
        self.assertEqual(mode, 'full')
        self.assertIn('no merge base', reason)

    def test_path_map_selects_node_ids_for_globbed_inputs(self) -> None:
        rules = self.repo.parent / f'{self.repo.name}-affected-map'
        rules.write_text('# comment\n^cases/([^/]+)/ tests/test_unrelated.py::test_case[\\1]\n', encoding='utf-8')
        self.addCleanup(rules.unlink)
        with mock.patch.object(affected, 'PATH_MAP_FILE', rules):
            _, tests, _ = self.change('cases/alpha/case.yaml', 'x: 1\n')
        self.assertEqual(tests, ['tests/test_unrelated.py::test_case[alpha]'])


if __name__ == '__main__':
    unittest.main()
