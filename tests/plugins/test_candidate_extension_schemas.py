"""Schema contracts for candidate extensions through Hermes's registry boundary."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[2] / "candidate-extensions"


def _module(name: str):
    path = ROOT / name / "__init__.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_candidate_tool_descriptions_are_json_strings():
    for name in ("goal-lifecycle", "review-candidate", "request-update", "memory-journal"):
        module = _module(name)
        schemas = [v for k, v in vars(module).items() if k.endswith("_SCHEMA") and isinstance(v, dict)]
        assert schemas, name
        for schema in schemas:
            assert isinstance(schema["description"], str), (name, schema.get("name"))
            assert isinstance(json.loads(json.dumps(schema))["description"], str)


def test_installed_candidate_tool_definitions_have_string_descriptions(tmp_path, monkeypatch):
    from scripts.install_candidate_extensions import install
    install(tmp_path, ROOT)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.plugins as plugins_mod
    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    plugins_mod.discover_plugins(force=True)
    from tools.registry import registry
    for tool_name in ("goal_set", "review_candidate", "request_update", "memory_undo", "memory_journal_list"):
        entry = registry.get_entry(tool_name)
        assert entry is not None
        assert isinstance(entry.schema["description"], str)
