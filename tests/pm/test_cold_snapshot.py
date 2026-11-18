"""The cold source fixture must resolve CLI imports even on hosts without the E2E toolchain."""
import ast
from importlib.machinery import PathFinder
from pathlib import Path

from tests.pm._cold_snapshot import copy_cold_source


def test_cold_snapshot_follows_transitive_imports_and_preserves_package_data(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    cli = source / "hermes_cli"
    cli.mkdir()
    (cli / "__init__.py").write_text("", encoding="utf-8")
    (cli / "main.py").write_text("from added import child\n", encoding="utf-8")
    added = source / "added"
    added.mkdir()
    (added / "__init__.py").write_text("", encoding="utf-8")
    (added / "child.py").write_text("from . import sibling\n", encoding="utf-8")
    (added / "sibling.py").write_text("import leaf\nimport third_party\n", encoding="utf-8")
    (added / "data.json").write_text("{}", encoding="utf-8")
    (source / "leaf.py").write_text("", encoding="utf-8")
    (source / "unrelated.py").write_text("", encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock"):
        (source / name).write_text("production recipe", encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    copy_cold_source(source, snapshot)
    assert {path.name for path in snapshot.iterdir()} == {
        "hermes_bootstrap.py", "hermes_cli", "added", "leaf.py",
    }
    assert (snapshot / "added" / "data.json").read_text(encoding="utf-8") == "{}"
    assert (snapshot / "added" / "sibling.py").is_file()


def test_cold_snapshot_resolves_cli_first_party_imports(tmp_path):
    source = Path(__file__).resolve().parents[2]
    snapshot = tmp_path / "source"
    copy_cold_source(source, snapshot)
    # This is an import-graph contract, not a check of source spelling or layout.
    # Check deferred imports too, so another CLI branch cannot silently drift.
    tree = ast.parse((source / "hermes_cli" / "main.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    for module in sorted(modules):
        paths = [str(snapshot)]
        original_paths = [str(source)]
        for part in module.split("."):
            original = PathFinder.find_spec(part, original_paths)
            if original is None:
                break  # Third-party module or an attribute, rather than a submodule.
            copied = PathFinder.find_spec(part, paths)
            assert copied is not None, f"cold snapshot cannot resolve {module}"
            original_paths = original.submodule_search_locations
            paths = copied.submodule_search_locations
            if original_paths is None:
                break
    assert not (snapshot / "pyproject.toml").exists()
    assert not (snapshot / "uv.lock").exists()
