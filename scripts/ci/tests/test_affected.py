"""Dependency-based test selection for the quick lane, on a real Git repository."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('affected', Path(__file__).resolve().parents[1] / 'affected.py')
affected = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(affected)

FILES = {
    'pyproject.toml': '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
    'pkg/__init__.py': '',
    'pkg/core.py': 'VALUE = 1\n',
    'pkg/mid.py': 'from pkg.core import VALUE\n',
    'pkg/lazy.py': 'def later():\n    from pkg import core\n    return core\n',
    'pkg/typed.py': 'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg import core\n',
    'pkg/other.py': 'X = 2\n',
    'data/settings_fixture.json': '{}\n',
    'tests/conftest.py': '',
    'tests/test_core.py': 'from pkg import core\n',
    'tests/test_mid.py': 'import pkg.mid\n',
    'tests/test_lazy.py': 'import pkg.lazy\n',
    'tests/test_typed.py': 'import pkg.typed\n',
    'tests/test_inline.py': 'def test_it():\n    from pkg.other import X\n',
    'tests/test_reads_fixture.py': 'PATH = "data/settings_fixture.json"\n',
    'tests/test_unrelated.py': 'def test_ok():\n    pass\n',
    'tests/sub/conftest.py': 'from pkg import other\n',
    'tests/sub/test_under_conftest.py': '',
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True, encoding='utf-8').stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, 'init', '-q', '-b', 'main')
    git(tmp_path, 'config', 'user.email', 'ci@example.invalid')
    git(tmp_path, 'config', 'user.name', 'ci')
    for path, text in FILES.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, encoding='utf-8')
    git(tmp_path, 'add', '-A')
    git(tmp_path, 'commit', '-qm', 'base')
    git(tmp_path, 'switch', '-qc', 'change')
    return tmp_path


def change(root: Path, path: str, text: str | None) -> tuple[str, list[str], str]:
    if text is None:
        git(root, 'rm', '-q', path)
    else:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding='utf-8')
        git(root, 'add', path)
    git(root, 'commit', '-qm', 'change')
    return affected.choose(root, 'main')


def test_selects_direct_and_transitive_importers(repo: Path) -> None:
    mode, tests, _ = change(repo, 'pkg/core.py', 'VALUE = 3\n')
    assert mode == 'affected'
    assert tests == ['tests/test_core.py', 'tests/test_mid.py']


def test_skips_deferred_and_annotation_only_imports(repo: Path) -> None:
    _, tests, _ = change(repo, 'pkg/core.py', 'VALUE = 3\n')
    assert 'tests/test_lazy.py' not in tests
    assert 'tests/test_typed.py' not in tests


def test_follows_imports_inside_test_bodies_and_conftest_scope(repo: Path) -> None:
    _, tests, _ = change(repo, 'pkg/other.py', 'X = 3\n')
    assert tests == ['tests/sub/test_under_conftest.py', 'tests/test_inline.py']


def test_selects_tests_that_name_a_changed_data_file(repo: Path) -> None:
    _, tests, _ = change(repo, 'data/settings_fixture.json', '{"a": 1}\n')
    assert tests == ['tests/test_reads_fixture.py']


def test_changed_test_runs_itself(repo: Path) -> None:
    _, tests, _ = change(repo, 'tests/test_unrelated.py', 'def test_ok():\n    assert True\n')
    assert tests == ['tests/test_unrelated.py']


def test_deleted_module_selects_its_importers(repo: Path) -> None:
    _, tests, _ = change(repo, 'pkg/mid.py', None)
    assert tests == ['tests/test_mid.py']


@pytest.mark.parametrize('path', ['tests/conftest.py', 'pyproject.toml', 'uv.lock', '.github/workflows/gate.yml'])
def test_shared_setup_runs_everything(repo: Path, path: str) -> None:
    mode, tests, reason = change(repo, path, '# changed\n')
    assert (mode, tests) == ('full', [])
    assert path in reason


def test_missing_base_runs_everything(repo: Path) -> None:
    mode, _, reason = affected.choose(repo, 'no-such-ref')
    assert mode == 'full'
    assert 'no merge base' in reason


def test_path_map_selects_node_ids_for_globbed_inputs(repo: Path, tmp_path_factory: pytest.TempPathFactory,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    rules = tmp_path_factory.mktemp('map') / 'affected-map'
    rules.write_text('# comment\n^cases/([^/]+)/ tests/test_unrelated.py::test_case[\\1]\n', encoding='utf-8')
    monkeypatch.setattr(affected, 'PATH_MAP_FILE', rules)
    _, tests, _ = change(repo, 'cases/alpha/case.yaml', 'x: 1\n')
    assert tests == ['tests/test_unrelated.py::test_case[alpha]']


def test_prose_beside_ci_configuration_does_not_run_everything(repo: Path) -> None:
    mode, tests, _ = change(repo, '.github/workflows/README.md', '# notes\n')
    assert (mode, tests) == ('affected', [])


def test_conftest_fixture_imports_count_even_when_deferred(repo: Path) -> None:
    git(repo, 'switch', '-q', 'main')
    (repo / 'tests/conftest.py').write_text('def fixture():\n    from pkg import core\n', encoding='utf-8')
    git(repo, 'commit', '-qam', 'fixture')
    git(repo, 'switch', '-qC', 'change')
    _, tests, _ = change(repo, 'pkg/core.py', 'VALUE = 3\n')
    assert 'tests/test_unrelated.py' in tests


def test_pytest_plugins_and_module_constant_imports_count(repo: Path) -> None:
    git(repo, 'switch', '-q', 'main')
    (repo / 'tests/test_plugin_user.py').write_text('pytest_plugins = ["pkg.other"]\n', encoding='utf-8')
    (repo / 'tests/test_dynamic.py').write_text(
        'import importlib\nTARGET = "pkg.other"\ndef test_x():\n    importlib.import_module(TARGET)\n', encoding='utf-8')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'plugins')
    git(repo, 'switch', '-qC', 'change')
    _, tests, _ = change(repo, 'pkg/other.py', 'X = 4\n')
    assert {'tests/test_plugin_user.py', 'tests/test_dynamic.py'} <= set(tests)


@pytest.mark.parametrize('path', ['rust/tool/Cargo.toml', 'tools/web/package.json'])
def test_nested_dependency_manifests_run_everything(repo: Path, path: str) -> None:
    mode, _, _ = change(repo, path, '# changed\n')
    assert mode == 'full'


def test_importorskip_counts_as_an_import(repo: Path) -> None:
    git(repo, 'switch', '-q', 'main')
    (repo / 'tests/test_optional.py').write_text(
        'import pytest\nother = pytest.importorskip("pkg.other")\n', encoding='utf-8')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'optional')
    git(repo, 'switch', '-qC', 'change')
    _, tests, _ = change(repo, 'pkg/other.py', 'X = 5\n')
    assert 'tests/test_optional.py' in tests
