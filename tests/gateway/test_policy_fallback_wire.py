"""Portable wire coverage for credential fallback on ordinary and frozen session routes.

The admission registry and unrelated presentation services are fixture seams. Credential
resolution, the scoped notice owner, TurnRunner, AIAgent and the HTTP client are real.
This complements, but does not replace, the Linux-gated subprocess provider suite.
"""
import json
import os
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from gateway.run_turn import GatewayTurnMixin
from gateway.run_turn_prepare import _resolve_policy_agent_runtime
from gateway.run_turn_runner import TurnRunner
from gateway.session import Platform, SessionSource
from gateway.session_policy import LocalSessionPolicy
from gateway.turn_context import TurnContext
from tests.e2e.core.providers.test_fallback_providers import _expired_nous_auth, _portal
from tests.fakes.fake_llm_provider import FakeLLMServer
from tests.gateway.test_pre_agent_fallback_notice import _runner_with_real_runtime_resolution


def _runner():
    runner = _runner_with_real_runtime_resolution()
    runner.session_authorities = runner.session_authority = None
    runner._gateway_loop = None
    owner = object.__new__(GatewayRunner)
    for key in ("session-a", "session-b"):
        owner._session_state(key)
    runner._session_state = owner._session_state
    runner._peek_session_state = owner._peek_session_state
    runner._resolve_session_key_or_none.side_effect = lambda source=None, session_key=None: session_key
    runner._last_attended_decision = None
    # A generic MagicMock here invents an attended decision and can accidentally heal the broken
    # route before HTTP dispatch. Bind the real disabled-attended path and real notice storage.
    for name in ("_consume_pre_agent_fallback_notice", "_handoff_attended_state",
                 "_attended_resolution_for_session", "_attended_route_for_session"):
        setattr(runner, name, types.MethodType(getattr(GatewayTurnMixin, name), runner))
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = {}
    runner._agent_config_signature = GatewayRunner._agent_config_signature
    return runner


def _policy(cfg, cwd):
    return LocalSessionPolicy(source="cli", platform="cli", cwd=str(cwd),
        model=cfg["model"]["default"], toolsets=(), config_json=json.dumps(cfg),
        request_json="{}", terminal_json="{}")


@pytest.mark.parametrize("bound", [False, True], ids=["ordinary", "session-bound"])
@pytest.mark.parametrize("fails", [False, True], ids=["primary-ok", "portal-503"])
def test_resolved_route_reaches_wire_and_reuses_prompt(monkeypatch, tmp_path, bound, fails):
    with (_portal("5xx") as (portal_url, hits),
          FakeLLMServer(default_text="FROM-PRIMARY") as primary,
          FakeLLMServer(default_text="FROM-FALLBACK") as fallback,
          FakeLLMServer(default_text="WRONG-FALLBACK") as later):
        primary_model = "Hermes-4-70B" if fails else "primary-model"
        cfg = {"model": {"provider": "nous" if fails else "custom", "default": primary_model,
                         "context_length": 128000},
               "fallback_providers": [
                   {"provider": "custom", "model": "fallback-model", "base_url": fallback.base_url},
                   {"provider": "custom", "model": "later-model", "base_url": later.base_url}]}
        if not fails:
            cfg["model"].update(base_url=primary.base_url, api_key="fake-loopback")
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(json.dumps(cfg))
        (home / "auth.json").write_text(json.dumps(_expired_nous_auth()))
        monkeypatch.setenv("OPENAI_API_KEY", "fake-loopback")
        monkeypatch.setenv("HERMES_PORTAL_BASE_URL", portal_url)
        # No request may reach a real vendor. Both token refresh and inference use loopback fakes.
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("HERMES_NOUS_TIMEOUT_SECONDS", "5")
        runner = _runner()
        policy = _policy(cfg, tmp_path) if bound else None
        monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda *_a: policy)
        monkeypatch.setattr("gateway.session_worker_construct.session_prefill", lambda *_a: None)
        monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *_a: primary_model)
        monkeypatch.setattr("gateway.run._get_channel_override", lambda *_a, **_k: None)
        built = []

        def agent_factory(**kwargs):
            from run_agent import AIAgent
            built.append(dict(kwargs))
            kwargs.update(skip_context_files=True, skip_memory=True, load_soul_identity=False)
            return AIAgent(**kwargs)

        ctx = TurnContext(source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
            message="CANARY fallback wire", history=[], session_id=None, session_key="session-a",
            user_config=cfg, AIAgent=agent_factory, enabled_toolsets=["memory"],
            resolve_display_setting=lambda *_a: False, _run_still_current=lambda: True,
            _hooks_ref=SimpleNamespace(loaded_hooks=False))
        turn = TurnRunner(runner, ctx)
        try:
            first = turn.run_sync()
            first_agent = ctx.agent_holder[0]
            second = turn.run_sync()
            server = fallback if fails else primary
            expected_model = "fallback-model" if fails else "primary-model"
            capture = {"bound": bound, "fails": fails, "portal_hits": list(hits),
                       "built": built, "requests": server.requests,
                       "result_models": [first.get("model"), second.get("model")],
                       "agent_model": ctx.agent_holder[0].model,
                       "agent_provider": ctx.agent_holder[0].provider,
                       "agent_base_url": ctx.agent_holder[0].base_url}
            (tmp_path / "wire.json").write_text(json.dumps(capture, indent=2, default=str))
            assert bool(hits) is fails
            if fails:
                assert "/api/oauth/token" in hits
                assert not primary.requests
            else:
                assert not fallback.requests
            assert not later.requests, "resolution must retain the configured fallback ordering"
            sent = server.main_requests()
            assert len(sent) == 2, capture
            assert [r["model"] for r in sent] == [expected_model, expected_model]
            assert first["model"] == second["model"] == expected_model
            assert first["final_response"] == second["final_response"] == (
                "FROM-FALLBACK" if fails else "FROM-PRIMARY")
            assert ctx.agent_holder[0] is first_agent and len(built) == 1
            assert first_agent.provider == "custom" and first_agent.base_url == server.base_url
            assert sent[0]["messages"][0] == sent[1]["messages"][0]
            assert sent[0]["tools"] == sent[1]["tools"]
            assert "Provider fallback:" not in json.dumps(sent[0]["messages"])
            assert runner._consume_pre_agent_fallback_notice("session-b") is None
        finally:
            agent = ctx.agent_holder[0]
            if agent is not None:
                agent.close()


@pytest.mark.parametrize("first_consumer", ["session-a", "session-b"])
def test_policy_notice_stays_with_resolved_session(monkeypatch, tmp_path, first_consumer):
    runner = _runner()
    cfg = {"model": {"provider": "nous", "default": "primary-model"}}
    policy = _policy(cfg, tmp_path)
    resolved = iter([
        ({"provider": "custom", "api_key": "fake-a", "base_url": "http://127.0.0.1:1/v1"},
         {"provider": "custom", "model": "fallback-a"}),
        ({"provider": "nous", "api_key": "fake-b"}, None)])
    monkeypatch.setattr("gateway.session_worker_construct.route_runtime", lambda *_a, **_k: next(resolved))
    model_a, _ = _resolve_policy_agent_runtime(runner, policy, session_key="session-a")
    model_b, _ = _resolve_policy_agent_runtime(runner, policy, session_key="session-b")
    assert model_a == "fallback-a" and model_b == "primary-model"
    other = "session-b" if first_consumer == "session-a" else "session-a"
    notices = {key: runner._consume_pre_agent_fallback_notice(key) for key in (first_consumer, other)}
    assert "custom/fallback-a" in notices["session-a"]
    assert notices["session-b"] is None
    assert runner._consume_pre_agent_fallback_notice("session-a") is None
    assert runner._peek_session_state("session-a").conversation.last_resolved_model == ""


@pytest.mark.parametrize("follow_profile", [False, True], ids=["frozen", "following-explicit"])
def test_local_explicit_reasoning_survives_live_attended_enablement(monkeypatch, tmp_path, follow_profile):
    """A profile edit cannot replace the LOCAL client's explicit launch reasoning at dispatch."""
    from dataclasses import replace

    with FakeLLMServer(default_text="LOCAL-POLICY") as server:
        frozen = {
            "model": {"provider": "custom", "default": "primary-model", "base_url": server.base_url,
                      "api_key": "fake-loopback", "context_length": 128000},
            "agent": {"reasoning_effort": "low", "max_turns": 2},
        }
        live = json.loads(json.dumps(frozen))
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(json.dumps(live))
        monkeypatch.setenv("OPENAI_API_KEY", "fake-loopback")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        runner = _runner()
        runner.session_authority = SimpleNamespace(db=SimpleNamespace(db_path=str(home / "state.db")),
                                                   sessions={})
        runner._session_reasoning_overrides = {}
        runner.session_store.get_reasoning_override.return_value = None
        # Restore production reasoning resolution rather than inherit MagicMock decisions from
        # the fallback harness. The policy selector and both request-boundary callbacks stay real.
        for name in ("_load_attended_reasoning_config", "_resolve_session_reasoning_config",
                     "_session_reasoning_override"):
            setattr(runner, name, types.MethodType(getattr(GatewayRunner, name), runner))
        runner._load_reasoning_config = GatewayRunner._load_reasoning_config
        # Exercise the inactive/default decision as well as feature enablement, without an idle
        # timer, a new-session stamp, or an attended-model switch obscuring the reasoning change.
        runner._session_state("session-a").conversation.last_user_activity_at = 0.0
        policy = replace(_policy(frozen, tmp_path), request_json=json.dumps({
            "reasoning": "low", "follow_profile_config": follow_profile, "toolsets": [],
        }))
        monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda *_a: policy)
        monkeypatch.setattr("gateway.session_worker_construct.session_prefill", lambda *_a: None)
        monkeypatch.setattr("gateway.run._load_gateway_config", lambda *_a, **_k: live)
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *_a: "primary-model")
        monkeypatch.setattr("gateway.run._get_channel_override", lambda *_a, **_k: None)
        built = []

        def agent_factory(**kwargs):
            from run_agent import AIAgent
            built.append(dict(kwargs))
            kwargs.update(skip_context_files=True, skip_memory=True, load_soul_identity=False)
            return AIAgent(**kwargs)

        ctx = TurnContext(source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
            message="CANARY explicit LOCAL reasoning", history=[], session_id=None, session_key="session-a",
            user_config=frozen, AIAgent=agent_factory, enabled_toolsets=["memory"],
            resolve_display_setting=lambda *_a: False, _run_still_current=lambda: True,
            _hooks_ref=SimpleNamespace(loaded_hooks=False))
        turn = TurnRunner(runner, ctx)
        try:
            first = turn.run_sync()
            first_agent = ctx.agent_holder[0]
            first_reasoning = dict(first_agent.reasoning_config or {})
            # The stored policy and the LOCAL turn's snapshot remain unchanged while the owning
            # profile enables attended routing and raises its default effort.
            live["agent"]["reasoning_effort"] = "high"
            live["model"]["attended"] = {"model": "attended-model", "reasoning_effort": "high"}
            (home / "config.yaml").write_text(json.dumps(live))
            second = turn.run_sync()
            agent = ctx.agent_holder[0]
            capture = {"follow_profile": follow_profile, "requests": server.requests,
                       "built_reasoning": [kw.get("reasoning_config") for kw in built],
                       "first_reasoning": first_reasoning, "second_reasoning": agent.reasoning_config,
                       "first_result": first, "second_result": second}
            (tmp_path / "local-reasoning-wire.json").write_text(json.dumps(capture, indent=2, default=str))
            sent = server.main_requests()
            assert len(sent) == 2, capture
            assert [request["model"] for request in sent] == ["primary-model", "primary-model"], capture
            assert first["final_response"] == second["final_response"] == "LOCAL-POLICY", capture
            assert first_reasoning == {"enabled": True, "effort": "low"}, capture
            assert agent.model == "primary-model" and agent.base_url == server.base_url, capture
            assert agent.reasoning_config == {"enabled": True, "effort": "low"}, capture
        finally:
            agent = ctx.agent_holder[0]
            if agent is not None:
                agent.close()
