"""Current named authority and frozen execution meet at real preflight boundaries."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tools import delegate_tool


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("hermes_cli.runtime_provider._try_resolve_from_custom_pool", lambda *a, **kw: None)
    config = {"providers": {name: {"base_url": f"https://{name}.invalid/v1",
        "api_key": f"fixture-{name}", "default_model": "shared-model"} for name in ("primary", "first", "second")}}

    def write():
        from hermes_cli import config as config_mod
        (tmp_path / "config.yaml").write_text(json.dumps(config))
        config_mod._LOAD_CONFIG_CACHE.clear()
        config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()

    write()
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="cli", profile_name="default")
    parent = SimpleNamespace(session_id="root", _session_db=db, provider="custom", model="parent-model",
                             base_url="https://parent.invalid/v1", api_key="fixture-parent",
                             api_mode="chat_completions", request_overrides={}, _fallback_chain=[])
    yield config, write, parent
    db.close()


def test_same_model_custom_primary_and_fallback_build_their_own_pinned_requests(configured, monkeypatch):
    from tests.agent.test_run_agent_codex_responses import _patch_agent_bootstrap
    _patch_agent_bootstrap(monkeypatch)
    config, write, parent = configured
    cfg = {"subagents": {"advisor": {"description": "Advise", "instructions": "Analyze",
        "provider": "custom:primary", "model": "shared-model",
        "fallbacks": [{"provider": "custom:first", "model": "shared-model"}]}}}
    launches, error = delegate_tool._preflight_task_runtime(
        [{"goal": "Inspect", "subagent_type": "advisor"}], cfg, None, parent, None)
    assert error is None
    launch = launches[0]
    child = delegate_tool._build_child_agent(
        0, "Inspect", None, None, launch.credentials["model"], 1, 1, parent,
        subagent_definition=launch.definition, resolved_reasoning=launch.reasoning,
        resolved_fallback_routes=launch.fallback_routes, **delegate_tool._creds_overrides(launch.credentials))
    try:
        for endpoint in ("primary", "first"):
            assert child.provider == ("custom:primary" if endpoint == "primary" else "custom")
            assert child.base_url.rstrip("/") == config["providers"][endpoint]["base_url"]
            kwargs = child._build_api_kwargs([{"role": "user", "content": "Inspect"}], tools_for_api=[])
            child._delegation_runtime_pin.validate_request(child, kwargs, client=child.client)
            if endpoint == "primary":
                assert child._try_activate_fallback()
    finally:
        child.close()


@pytest.mark.parametrize("collision", ["primary", "fallback"])
def test_duplicate_resolved_fallbacks_are_rejected_in_preflight(configured, collision):
    _, _, parent = configured
    cfg = {"subagents": {"advisor": {"description": "Advise", "instructions": "Analyze",
        "provider": "custom:primary", "model": "primary-model",
        "fallbacks": [{"provider": "custom:" + name, "model": "shared-model"} for name in ("first", "second")]}}}
    if collision == "primary":
        parent.model = "shared-model"
        role = cfg["subagents"]["advisor"]
        role.pop("provider")
        role.pop("model")
        role["inherit_parent"] = True
    launches, error = delegate_tool._preflight_task_runtime(
        [{"goal": "Inspect", "subagent_type": "advisor"}], cfg, None, parent, None)
    assert launches == []
    assert "repeats a resolved provider/model identity" in error


@pytest.mark.parametrize("change", [None, "default", "provider", "allowlist", "allowed", "inherited"])
def test_moa_resume_retains_snapshot_but_honors_explicit_role_revocation(configured, change):
    config, write, parent = configured
    preset = {"reference_models": [{"provider": "custom:primary", "model": "reference"}],
              "aggregator": {"provider": "custom:first", "model": "aggregate"}}
    config["moa"] = {"presets": {"review": preset, "edited-review": deepcopy(preset)}}
    write()
    role = {"description": "Review", "instructions": "Review carefully", "provider": "moa", "model": "review"}
    cfg = {"subagents": {"council": role}}
    launches, error = delegate_tool._preflight_task_runtime(
        [{"goal": "Review", "subagent_type": "council"}], cfg, None, parent, None)
    assert error is None
    frozen = launches[0].moa_snapshot.metadata()
    metadata = {"version": 1, "subagent_type": "council", "description": "Review", "instructions": "Review carefully",
        "parent_session_root": "root", "provider": "moa", "model": "review", "base_url": "moa://local",
        "api_mode": "chat_completions", "reasoning_effort": None, "fallbacks": [], "enabled_toolsets": [],
        "request_overrides": {}, "moa": frozen}
    if change == "allowlist":
        # The preset identity, not the separately stored model label, owns selection.
        metadata["model"] = "edited-review"
    db = parent._session_db
    db.create_session("child", source="tool", profile_name="default", model_config={
        "_delegation_launch": metadata, "_delegate_from": "root", "_delegation_completed": True})
    db.append_message("child", role="user", content="Original review")
    if change == "provider":
        role.update(provider="custom:primary", model="shared-model")
    elif change == "inherited":
        role.pop("provider")
        role.pop("model")
        role["inherit_parent"] = True
        parent.provider = "moa"
        parent.model = "edited-review"
    elif change in {"default", "allowlist", "allowed"}:
        role["model"] = "edited-review"
        if change != "default":
            role["moa_presets"] = ["edited-review"] + (["review"] if change == "allowed" else [])
    before = db.get_session("child")["model_config"]
    resumed, error = delegate_tool._preflight_task_runtime(
        [{"goal": "Continue review", "resume_session_id": "child"}], cfg, None, parent, None)
    try:
        if change in {"provider", "allowlist"}:
            assert resumed == []
            assert "current named MoA role" in error
            assert db.get_session("child")["model_config"] == before
        else:
            assert error is None
            assert resumed[0].credentials["model"] == "review"
            assert resumed[0].moa_snapshot.metadata() == frozen
            assert resumed[0].launch_metadata == metadata
    finally:
        delegate_tool._release_resume_launches(parent, resumed)
