"""Fork runtime contracts cross the native provider seam, not a test-only builder."""

import copy
import json
from unittest.mock import MagicMock, patch

import pytest

from tools import delegate_tool as delegate
from tools import delegate_tool_routes as routes
from tests.tools.test_delegate_by_parent import (
    HIGH,
    MEDIUM,
    SOL,
    VENICE,
    _authored,
    _parent,
    config_file as _config_file,
)

config_file = _config_file


def _spawn(parent, **options):
    built = []
    children = []

    def construct(**kwargs):
        built.append(kwargs)
        child = MagicMock(
            reasoning_config=copy.deepcopy(kwargs["reasoning_config"]),
            reasoning_override=None,
            _session_db=kwargs["session_db"],
        )
        children.append(child)
        return child

    with (
        patch("run_agent.AIAgent", side_effect=construct),
        patch.object(delegate, "_run_batch", return_value=json.dumps({"results": [], "summary": "done"})),
    ):
        result = json.loads(delegate.delegate_task(
            tasks=[{"goal": "Inspect the resolved child runtime"}],
            parent_agent=parent,
            **options,
        ))
    assert "error" not in result, result
    return built[0], children[0]


def test_default_backend_selects_by_parent_before_native_provider_and_real_builder(config_file):
    raw = _authored()
    raw["delegation"]["max_iterations"] = 19
    config_file(raw)
    parent = _parent()
    with (
        patch.object(routes, "_active_provider", wraps=routes._active_provider) as provider,
        patch.object(routes, "_route_one", wraps=routes._route_one) as route,
        patch.object(delegate, "_resolve_child_runtime", wraps=delegate._resolve_child_runtime) as runtime,
    ):
        built, _ = _spawn(parent)
    assert provider.call_count == route.call_count == runtime.call_count == 1
    assert route.call_args.args[0].name == "default"
    selected = route.call_args.args[4]
    assert "provider" not in selected and "model" not in selected
    assert selected["max_iterations"] == 19
    assert runtime.call_args.args[1] == selected
    assert (built["model"], built["provider"], built["base_url"]) == (
        VENICE, parent.provider, parent.base_url,
    )
    assert built["reasoning_config"] == MEDIUM


@pytest.mark.parametrize("credentials_cfg", [{}, {"model": "review-model"}])
def test_internal_review_bypasses_provider_but_uses_same_real_builder(config_file, credentials_cfg):
    with (
        patch.object(routes, "_active_provider", wraps=routes._active_provider) as provider,
        patch.object(delegate, "_resolve_child_runtime", wraps=delegate._resolve_child_runtime) as runtime,
    ):
        built, _ = _spawn(_parent(), credentials_cfg=credentials_cfg)
    provider.assert_not_called()
    assert runtime.call_count == 1
    assert runtime.call_args.args[1] == delegate._load_config()
    assert built["reasoning_config"] == HIGH


@pytest.mark.parametrize("reasoning", [False, {"enabled": False}, {"enabled": True, "effort": "low"}])
def test_explicit_reasoning_survives_provider_routing_and_nested_marker(config_file, reasoning):
    parent = _parent(SOL)
    parent.reasoning_config = copy.deepcopy(reasoning)
    parent.reasoning_override = copy.deepcopy(reasoning)
    built, child = _spawn(parent)
    assert built["reasoning_config"] == reasoning
    assert child.reasoning_override == reasoning
    if isinstance(reasoning, dict):
        assert child.reasoning_override is not child.reasoning_config


@pytest.mark.parametrize("mode", ["priority", "auto", "cold"])
def test_fast_mode_reaches_real_pinned_child_without_parent_wire_leak(config_file, mode):
    raw = _authored()
    raw["delegation"]["inherit_service_tier"] = True
    config_file(raw)
    parent = _parent(SOL)
    parent.service_tier = mode
    parent.request_overrides = {
        "service_tier": "priority", "speed": "fast", "temperature": 0.25,
        "extra_body": {"service_tier": "priority", "speed": "fast"},
    }
    built, _ = _spawn(parent)
    assert built["service_tier"] == mode
    assert built["request_overrides"] == raw["delegation"]["request_overrides"]
    assert "service_tier" not in built["request_overrides"]
    assert "speed" not in built["request_overrides"]
    assert built["model"] == SOL


def test_inherited_child_sanitizes_nested_fast_fields_without_mutating_parent(config_file):
    parent = _parent()
    parent.request_overrides = {
        "service_tier": "priority", "speed": "fast", "temperature": 0.25,
        "extra_body": {"service_tier": "priority", "speed": "fast", "other": True},
    }
    original = copy.deepcopy(parent.request_overrides)
    built, _ = _spawn(parent)
    assert built["service_tier"] is None
    assert built["request_overrides"] == {"temperature": 0.25, "extra_body": {"other": True}}
    assert parent.request_overrides == original


@pytest.mark.parametrize("depth, expected", [(0, "orchestrator"), (1, "orchestrator"), (2, "leaf")])
def test_provider_spawn_keeps_depth_derived_live_model_role(config_file, depth, expected):
    raw = _authored()
    raw["delegation"]["max_spawn_depth"] = 3
    config_file(raw)
    parent = _parent()
    parent._delegate_depth = depth
    _, child = _spawn(parent)
    assert child._delegate_depth == depth + 1
    assert child._delegate_role == expected
    assert delegate._get_max_spawn_depth() == 3


def test_provider_child_keeps_its_own_handle_on_parent_profile_database(config_file, tmp_path):
    from hermes_state import SessionDB
    from hermes_state_registry import release

    db = SessionDB(tmp_path / "parent-profile.db")
    db.create_session("parent", "cli")
    parent = _parent()
    parent.session_id = "parent"
    parent._session_db = db
    child_db = None
    try:
        built, _ = _spawn(parent)
        child_db = built["session_db"]
        assert child_db is not db
        assert child_db.db_path == db.db_path
        db.close()
        child_db.create_session("surviving-child", "cli", parent_session_id="parent")
        assert child_db.get_session("surviving-child")["parent_session_id"] == "parent"
    finally:
        if child_db is not None:
            release(child_db)
        db.close()
