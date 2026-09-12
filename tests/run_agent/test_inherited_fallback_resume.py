"""Real parent-chain resolver → launch metadata → SessionDB → resume authority."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from tools import delegate_tool
from tools.custom_subagents import parse_definitions
from tools.custom_subagent_fallbacks import freeze_parent_fallback_routes


@pytest.fixture
def inherited_child(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    db = SessionDB(db_path=tmp_path / "state.db")
    parent = SimpleNamespace(
        session_id="root", _session_db=db, provider="primary", model="main",
        api_mode="chat_completions", api_key="PRIMARY-SENTINEL", base_url="https://primary.invalid/v1",
        request_overrides={}, prefill_messages=None, _delegate_depth=0,
        _fallback_chain=[{"provider": "custom", "model": "backup", "api_mode": "chat_completions",
                          "base_url": "http://[::1]:8000/v1", "api_key": "FALLBACK-SENTINEL",
                          "request_overrides": {"extra_headers": {"Authorization": "HEADER-SENTINEL"},
                                                "max_tokens": 321}}],
    )
    definition = parse_definitions({"subagents": {"owner": {
        "description": "Own", "instructions": "Own task", "inherit_parent": True,
    }}})["owner"]
    primary = dict(provider="primary", model="main", base_url=parent.base_url,
                   api_key=parent.api_key, api_mode=parent.api_mode, request_overrides={})
    calls = []
    def runtime(**kwargs):
        calls.append(kwargs)
        if kwargs["requested"] == "primary":
            return dict(primary)
        if kwargs.get("explicit_base_url") and kwargs.get("explicit_api_key"):
            return dict(provider="custom", model=kwargs["target_model"],
                        base_url=kwargs["explicit_base_url"], api_key=kwargs["explicit_api_key"],
                        api_mode="chat_completions", request_overrides={})
        raise ValueError("No global custom-provider authority")
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", runtime)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    child_runtime = {k: v for k, v in primary.items() if k != "request_overrides"}
    monkeypatch.setattr(delegate_tool, "_resolve_child_runtime",
                        lambda *_a, **_k: {**child_runtime, "fallback_model": None})
    monkeypatch.setattr(delegate_tool, "_resolve_child_toolsets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(delegate_tool, "_open_child_session_db", lambda _: None)
    monkeypatch.setattr(delegate_tool, "_attach_child", lambda *_a: None)
    class Child:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self._session_init_model_config = {}
    monkeypatch.setattr("run_agent.AIAgent", Child)
    routes = freeze_parent_fallback_routes(parent, parent.provider, parent.model)
    child = delegate_tool._build_child_agent(
        0, "inspect", None, None, "main", 1, 1, parent, subagent_definition=definition,
        resolved_fallback_routes=routes,
    )
    launch = child._delegation_launch_metadata
    db.create_session("root", "cli", profile_name="default")
    def persist(launch, active=False):
        # Each test persists once; a stable id keeps the test about durable ownership.
        sid = "child"
        db.create_session(sid, "delegate", profile_name="default", model_config={
            "_delegate_from": "root", "_delegation_launch": deepcopy(launch), "_delegation_completed": True,
            **({"_delegation_active_route": {"provider": "custom", "model": "backup"}} if active else {}),
        })
        return sid
    yield parent, definition, launch, persist, calls
    db.close()


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("edited_role", [False, True])
def test_inherited_fallback_resumes_from_original_parent_authority(inherited_child, active, edited_role):
    from dataclasses import replace
    parent, definition, launch, persist, calls = inherited_child
    sid = persist(launch, active)
    if edited_role:
        definition = replace(definition, inherit_parent=False, provider="primary", model="main", fallbacks=())
    restored = delegate_tool._resolve_resume_launch({"resume_session_id": sid}, {"owner": definition}, parent)
    assert launch["fallback_source"] == "parent"
    if active:
        assert restored.fallback_routes == ()  # active fallback is consumed, not retried
        assert restored.credentials["api_key"] == "FALLBACK-SENTINEL"
        assert restored.credentials["base_url"] == "http://[::1]:8000/v1"
        assert restored.credentials["request_overrides"]["extra_headers"]["Authorization"] == "HEADER-SENTINEL"
    else:
        route = restored.fallback_routes[0]
        assert route.base_url == "http://[::1]:8000/v1"
        assert route.api_key == "FALLBACK-SENTINEL"
        assert json.loads(route.request_overrides_json)["extra_headers"]["Authorization"] == "HEADER-SENTINEL"
    stored = parent._session_db.get_session(sid)["model_config"]
    assert all(secret not in stored for secret in ("PRIMARY-SENTINEL", "FALLBACK-SENTINEL", "HEADER-SENTINEL"))


@pytest.mark.parametrize("change", ["endpoint", "key", "overrides", "model", "mode", "missing", "owner", "legacy"])
def test_inherited_fallback_resume_rejects_changed_or_unproven_authority(inherited_child, change):
    parent, definition, launch, persist, calls = inherited_child
    if change == "legacy":
        launch.pop("fallback_source", None)
    sid = persist(launch)
    fallback = parent._fallback_chain[0]
    if change == "endpoint": fallback["base_url"] = "https://different.invalid/v1"
    elif change == "key": fallback["api_key"] = "DIFFERENT-SENTINEL"
    elif change == "overrides": fallback["request_overrides"]["extra_headers"]["Authorization"] = "DIFFERENT-SENTINEL"
    elif change == "model": fallback["model"] = "different"
    elif change == "mode": fallback["api_mode"] = "anthropic_messages"
    elif change == "missing": parent._fallback_chain = []
    elif change == "owner": parent.session_id = "other-owner"
    with pytest.raises(ValueError):
        delegate_tool._resolve_resume_launch({"resume_session_id": sid}, {"owner": definition}, parent)
