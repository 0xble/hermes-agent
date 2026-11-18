"""First-party import closure for the cold CLI's source-only fixture.

Packages are atomic: PM dispatches workers by module name, and CLI/plugin code
loads sibling modules and data lazily. Scan all imports (including deferred ones)
in each reached package, without importing application code or third-party deps.
"""
import ast
from pathlib import Path
import shutil


def cold_source_roots(source: Path) -> set[Path]:
    """Resolve the CLI entry modules to a closure of repository packages/modules."""
    roots = {path.stem: path for path in source.glob("*.py") if path.name != "setup.py"}
    roots.update({path.name: path for path in source.iterdir()
                  if path.is_dir() and (path / "__init__.py").is_file()})
    pending = {"hermes_bootstrap", "hermes_cli"}
    reached = set()
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        root = roots[name]
        files = root.rglob("*.py") if root.is_dir() else [root]
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                imports = []
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    imports = [node.module]
                pending.update(module.split(".")[0] for module in imports
                               if module.split(".")[0] in roots
                               and module.split(".")[0] not in reached)
    return {roots[name] for name in reached}


def copy_cold_source(source: Path, destination: Path) -> None:
    destination.mkdir()
    for root in cold_source_roots(source):
        target = destination / root.name
        if root.is_dir():
            shutil.copytree(root, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(root, target)
