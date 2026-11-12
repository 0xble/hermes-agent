"""Complete preset policy survives normalization and real consumer failure paths."""
from copy import deepcopy
from types import SimpleNamespace

import hermes_yaml as yaml
import pytest

BACKUP = {"provider": "openrouter", "model": "backup", "reasoning_effort": "none"}
PRIMARY = {"provider": "openrouter", "model": "primary", "reasoning_effort": "high", "fallbacks": [BACKUP]}


def write_config(tmp_path, monkeypatch, cfg):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


@pytest.mark.parametrize("shape", ["@p", {"model": "@p"}, {"model_preset": "p"}])
def test_moa_shorthand_reaches_runtime_normalizer(shape):
    from hermes_cli.model_presets import expand_model_presets
    from hermes_cli.moa_config import resolve_moa_preset
    cfg = {"model_presets": {"p": {k: v for k, v in PRIMARY.items() if k != "fallbacks"}},
           "moa": {"reference_models": [shape], "aggregator": shape}}
    preset = resolve_moa_preset(expand_model_presets(cfg)["moa"])
    for slot in [*preset["reference_models"], preset["aggregator"]]:
        assert (slot["provider"], slot["model"]) == ("openrouter", "primary")
    inline = {"provider": "openrouter", "model": "primary", "fallback_models": [shape]}
    cfg["moa"] = {"reference_models": [inline], "aggregator": inline}
    preset = resolve_moa_preset(expand_model_presets(cfg)["moa"])
    assert preset["aggregator"]["fallback_models"][0]["model"] == "primary"


@pytest.fixture
def moa_transport(tmp_path, monkeypatch):
    from agent import auxiliary_client as aux, moa_loop as moa
    cfg = {"model": {"provider": "openrouter", "default": "global"},
           "model_presets": {"p": PRIMARY},
           "auxiliary": {"transient_retries": 0},
           "moa": {"reference_models": [{"model_preset": "p"}], "aggregator": {"model_preset": "p"}}}
    write_config(tmp_path, monkeypatch, cfg)
    calls = []
    progress = []
    def create(**kw):
        calls.append(kw)
        if kw["model"] != "backup":
            raise TimeoutError("fixture primary timed out")
        aux._notify_aux_progress()
        return SimpleNamespace(model="backup", choices=[SimpleNamespace(
            message=SimpleNamespace(role="assistant", content="backup executed", tool_calls=None),
            finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7))
    client = SimpleNamespace(base_url="https://openrouter.ai/api/v1", api_key="fixture",
                             chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(aux, "_get_cached_client", lambda provider, model=None, **kw: (client, model))
    monkeypatch.setattr(aux, "resolve_provider_client", lambda provider, model=None, **kw: (client, model))
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda *a, **kw: False)
    monkeypatch.setattr(aux, "_mark_provider_unhealthy", lambda *a, **kw: None)
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **kw: False)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **kw: None)
    monkeypatch.setattr(aux, "_transient_retry_count", lambda: 0)
    monkeypatch.setattr(moa, "_slot_runtime", lambda slot: {
        "provider": slot["provider"], "model": slot["model"], "base_url": str(client.base_url), "api_key": "fixture"})
    agent = SimpleNamespace(_touch_activity=lambda desc: progress.append(desc))
    return cfg, calls, progress, agent


@pytest.mark.parametrize("consumer", ["reference", "aggregator", "streaming"])
def test_moa_executes_declared_backup(moa_transport, consumer):
    from agent import moa_loop as moa
    from hermes_cli.model_presets import expand_model_presets
    from hermes_cli.moa_config import resolve_moa_preset
    cfg, calls, progress, agent = moa_transport
    preset = resolve_moa_preset(expand_model_presets(cfg)["moa"])
    messages = [{"role": "user", "content": "run"}]
    if consumer == "reference":
        _label, text, acct = moa._run_reference(preset["reference_models"][0], messages, agent=agent)
        assert text == "backup executed"
        assert (acct.model, acct.provider) == ("backup", "openrouter")
        assert acct.usage.input_tokens == 5
        assert progress
    else:
        facade = moa.MoAChatCompletions("default", agent=agent)
        result = facade._call_prepared_aggregator({"aggregator": preset["aggregator"],
            "messages": messages, "guidance": None, "aggregator_temperature": None},
            {"stream": consumer == "streaming"})
        if consumer == "streaming":
            assert "backup executed" in "".join(str(c.choices[0].delta.content or "") for c in result)
        else:
            assert moa._extract_text(result) == "backup executed"
        assert facade.last_aggregator_slot["model"] == "backup"
    assert calls[0]["model"] == "primary"
    assert calls[-1]["model"] == "backup"
    assert calls[-1].get("extra_body", {}).get("reasoning", {}).get("enabled") is False


@pytest.mark.parametrize("consumer", ["reference", "aggregator", "streaming"])
@pytest.mark.parametrize("recovery", ["rotation", "refresh", "transient"])
@pytest.mark.parametrize("effort", ["none", False])
def test_moa_selected_destination_survives_same_route_recovery(moa_transport, monkeypatch, consumer, recovery, effort):
    from agent import auxiliary_client as aux, moa_loop as moa
    _cfg, calls, progress, agent = moa_transport
    recovered = []
    # Streaming uses the existing pre-emission rate-limit rung, not completion backoff.
    stream_retry = consumer == "streaming" and recovery == "transient"
    class Failure(RuntimeError):
        status_code = (429 if stream_retry else 500) if recovery == "transient" else 401
    monkeypatch.setattr(aux._aux_progress, "hook", lambda: progress.append("progress"), raising=False)
    def create(**kw):
        calls.append(deepcopy(kw))
        if len(calls) == 1:
            raise Failure("fixture selected destination recovery")
        aux._notify_aux_progress()
        return SimpleNamespace(model=kw["model"], choices=[SimpleNamespace(
            message=SimpleNamespace(role="assistant", content="done", tool_calls=None), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7))
    client = SimpleNamespace(base_url="https://openrouter.ai/api/v1", api_key="fixture",
                             chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(aux, "_get_cached_client", lambda provider, model=None, **kw:
                        (None, None) if model == "primary" else (client, model))
    monkeypatch.setattr(aux, "resolve_provider_client", lambda provider, model=None, **kw: (client, model))
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda provider, **kw:
                        recovered.append(provider) is None if recovery == "refresh" else False)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **kw: "openrouter" if recovery == "rotation" or stream_retry else None)
    monkeypatch.setattr(aux, "_recover_provider_pool", lambda provider, *a, **kw: recovered.append(provider) is None)
    monkeypatch.setattr(aux, "_transient_retry_count", lambda: 1)
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0)
    monkeypatch.setattr(moa, "_slot_runtime", lambda slot: {"provider": slot["provider"], "model": slot["model"]})
    monkeypatch.setattr(moa, "_reference_context_length", lambda slot, *a: 100000 if slot["model"] == "primary" else 1500)
    slot = {"provider": "anthropic", "model": "primary", "reasoning_effort": "high",
            "fallback_models": [{**BACKUP, "reasoning_effort": effort}]}
    messages = [{"role": "user", "content": "old " * 2500}, {"role": "assistant", "content": "old answer " * 2500},
                {"role": "user", "content": "latest"}]
    if consumer == "reference":
        _, text, acct = moa._run_reference(slot, messages, max_tokens=64, agent=agent)
        assert text == "done"
        assert (acct.model, acct.provider) == ("backup", "openrouter")
        assert acct.messages == calls[-1]["messages"]
        assert acct.usage.input_tokens == 5
        assert len(str(calls[0]["messages"])) < len(str(messages))
    else:
        facade = moa.MoAChatCompletions("default", agent=agent)
        response = facade._call_prepared_aggregator(
            {"aggregator": slot, "messages": messages, "guidance": None, "aggregator_temperature": None},
            {"stream": consumer == "streaming", "max_tokens": 64})
        text = "".join(str(c.choices[0].delta.content or "") for c in response) if consumer == "streaming" else moa._extract_text(response)
        assert text == "done"
        assert (facade.last_aggregator_slot["model"], facade.last_aggregator_slot["provider"]) == ("backup", "openrouter")
    assert len(calls) == 2
    assert all(c["model"] == "backup" and c["extra_body"]["reasoning"]["enabled"] is False for c in calls)
    assert calls[0]["messages"] == calls[1]["messages"]
    assert calls[0].get("tools") == calls[1].get("tools")
    assert recovered == ([] if recovery == "transient" else ["openrouter"])
    assert progress


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("recovery", ["rotation", "refresh", "transient"])
@pytest.mark.parametrize("next_destination", [False, True])
def test_selected_recovery_keeps_destination_plan_but_next_hop_uses_logical_input(
    moa_transport, monkeypatch, async_mode, recovery, next_destination,
):
    import asyncio
    from agent import auxiliary_client as aux
    from agent.auxiliary_call_policy import CallFallbackPolicy, scoped_fallback_policy
    _cfg, calls, _progress, _agent = moa_transport
    planned, recovered = [], []
    messages = [{"role": "user", "content": "canonical input"}]
    tools = [{"type": "function", "function": {"name": "canonical_tool", "parameters": {"type": "object"}}}]
    chain = [{**BACKUP, "timeout": 11}, {"provider": "openai", "model": "gpt-5-mini", "reasoning_effort": "low"}]
    class Failure(RuntimeError):
        status_code = 500 if recovery == "transient" else 401
    class Unavailable(RuntimeError):
        status_code = 402
    def create(**kw):
        calls.append(deepcopy(kw))
        if len(calls) == 1:
            raise Failure("fixture recovery")
        if kw["model"] == "backup" and next_destination:
            raise Unavailable("fixture destination exhausted")
        return SimpleNamespace(model=kw["model"], choices=[SimpleNamespace(
            message=SimpleNamespace(role="assistant", content="done", tool_calls=None), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7))
    async def acreate(**kw):
        return create(**kw)
    client = SimpleNamespace(base_url="https://openrouter.ai/api/v1", api_key="fixture",
                             chat=SimpleNamespace(completions=SimpleNamespace(create=acreate if async_mode else create)))
    second = SimpleNamespace(base_url="https://api.openai.com/v1", api_key="fixture",
                             chat=client.chat)
    monkeypatch.setattr(aux, "_get_cached_client", lambda provider, model=None, **kw:
                        (None, None) if model == "primary" else (second if model == "gpt-5-mini" else client, model))
    monkeypatch.setattr(aux, "resolve_provider_client", lambda provider, model=None, **kw:
                        (second if model == "gpt-5-mini" else client, model))
    monkeypatch.setattr(aux, "_to_async_client", lambda client, model, **kw: (client, model))
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda provider, **kw:
                        recovered.append(provider) is None if recovery == "refresh" else False)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **kw: "openrouter" if recovery == "rotation" else None)
    monkeypatch.setattr(aux, "_recover_provider_pool", lambda provider, *a, **kw: recovered.append(provider) is None)
    monkeypatch.setattr(aux, "_transient_retry_count", lambda: 1)
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0)
    cache = aux._replan_synchronous_cache_sections
    def replan(messages, tools, *, destination):
        planned.append((destination.model, deepcopy(messages), deepcopy(tools)))
        return cache(messages, tools, destination=destination)
    monkeypatch.setattr(aux, "_replan_synchronous_cache_sections", replan)
    def prepare(destination, original_messages, original_tools, reasoning, entry):
        assert original_messages == messages and original_tools == tools
        return [{"role": "user", "content": destination.model + " input"}], [], reasoning
    route = {}
    kwargs: dict = dict(task="moa_reference", provider="anthropic", model="primary", messages=messages,
                        tools=tools, reasoning_config={"enabled": True, "effort": "high"}, timeout=37, route_info=route)
    with scoped_fallback_policy(CallFallbackPolicy("moa_reference", chain, prepare)):
        result = asyncio.run(aux.async_call_llm(**kwargs)) if async_mode else aux.call_llm(
            **kwargs, extra_headers={"x-initiator": "user"}, fallback_chain=chain, fallback_request_transform=prepare)
    assert result.choices[0].message.content == "done"
    assert [c["model"] for c in calls] == (["backup", "backup", "gpt-5-mini"] if next_destination else ["backup", "backup"])
    assert all(c["messages"] == [{"role": "user", "content": "backup input"}]
               and c["extra_body"]["reasoning"]["enabled"] is False and c["timeout"] == 11 for c in calls[:2])
    assert calls[0].get("tools") == calls[1].get("tools")
    if not async_mode:
        assert calls[0]["extra_headers"] == calls[1]["extra_headers"] == {"x-initiator": "user"}
    assert all(m == messages and t == tools for _, m, t in planned)
    if next_destination:
        assert calls[-1]["messages"] == [{"role": "user", "content": "gpt-5-mini input"}]
        assert calls[-1]["extra_body"]["reasoning"]["effort"] == "low"
    assert {key: route[key] for key in ("provider", "model")} == {
        "provider": "openai" if next_destination else "openrouter",
        "model": "gpt-5-mini" if next_destination else "backup",
    }
    assert recovered == ([] if recovery == "transient" else ["openrouter"] * (2 if recovery == "rotation" and next_destination else 1))


@pytest.mark.parametrize("shape", [{"model_preset": "p", "fallbacks": []}, {"default": "@p", "fallbacks": []}])
def test_cron_default_optout_reaches_setup_and_execution(tmp_path, monkeypatch, shape):
    from cron import scheduler
    from hermes_cli.auth import AuthError
    from hermes_cli.model_presets import expand_model_presets
    from hermes_cli.model_preset_runtime import resolve_model_ref
    cfg = {"model": shape, "model_presets": {"p": PRIMARY}}
    write_config(tmp_path, monkeypatch, cfg)
    job = {"id": "proof", "name": "proof", "prompt": "run", "model": "@default"}
    jc = scheduler._resolve_cron_job_config(job, cfg)
    assert not jc.route.fallbacks
    assert resolve_model_ref(shape, cfg).fallbacks == ()
    assert resolve_model_ref("@default", expand_model_presets(cfg)).fallbacks == ()
    assert not scheduler._job_fallback_chain(job, jc.cfg, jc.route)
    visited = []
    def reject(**kw):
        visited.append(kw["target_model"])
        raise AuthError("fixture no credentials")
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", reject)
    setup = scheduler._resolve_cron_agent_setup(job, "proof", "proof", jc)
    assert setup.blocked is not None and setup.blocked[0] is False
    assert visited == ["primary"]
    with pytest.raises(RuntimeError, match="fixture no credentials"):
        scheduler._resolve_job_runtime(job, "proof", jc)
    assert visited == ["primary", "primary"]


def test_gateway_context_pin_belongs_to_named_endpoint(tmp_path, monkeypatch):
    from gateway import run
    cfg = {"model": {"model_preset": "p", "context_length": 200000},
           "model_presets": {"p": {"provider": "custom:a", "model": "same"}},
           "providers": {name: {"base_url": f"https://{name}.invalid/v1", "api_key": "fixture"} for name in ("a", "b")}}
    write_config(tmp_path, monkeypatch, cfg)
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **kw: kw["config_context_length"] or 8192)
    for name, expected in [("a", 200000), ("b", 8192)]:
        context = run._resolve_gateway_model_context("same", route={"provider": "custom", "base_url": f"https://{name}.invalid/v1"})
        assert context.context_length == expected


@pytest.mark.parametrize("consumer", ["reference", "aggregator", "streaming"])
def test_moa_empty_chain_disables_backup_execution(moa_transport, consumer):
    cfg, calls, _progress, agent = moa_transport
    cfg["moa"]["reference_models"][0]["fallbacks"] = []
    cfg["moa"]["aggregator"]["fallbacks"] = []
    from agent import moa_loop as moa
    from hermes_cli.model_presets import expand_model_presets
    from hermes_cli.moa_config import resolve_moa_preset
    preset = resolve_moa_preset(expand_model_presets(cfg)["moa"])
    slot = preset["aggregator"]
    assert slot["fallback_models"] == []
    messages = [{"role": "user", "content": "run"}]
    if consumer == "reference":
        _, text, _ = moa._run_reference(preset["reference_models"][0], messages, agent=agent)
        assert text.startswith("[failed:")
    else:
        with pytest.raises(TimeoutError, match="fixture primary"):
            result = moa.MoAChatCompletions("default", agent=agent)._call_prepared_aggregator(
                {"aggregator": slot, "messages": messages, "guidance": None, "aggregator_temperature": None},
                {"stream": consumer == "streaming"})
            if consumer == "streaming":
                list(result)
    assert [c["model"] for c in calls] == ["primary"]


def test_streaming_fallback_never_replays_emitted_content(moa_transport, monkeypatch):
    from agent import auxiliary_client as aux, moa_loop as moa
    _cfg, calls, _progress, agent = moa_transport
    def stream(*a, **kw):
        calls.append({"model": "primary"})
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="already emitted"))])
        raise TimeoutError("fixture after content")
    monkeypatch.setattr(aux, "_relay_sync_stream", stream)
    iterator = moa.MoAChatCompletions("default", agent=agent)._call_prepared_aggregator(
        {"aggregator": {**PRIMARY, "fallback_models": [BACKUP]}, "messages": [{"role": "user", "content": "run"}],
         "guidance": None, "aggregator_temperature": None}, {"stream": True})
    assert next(iterator).choices[0].delta.content == "already emitted"
    with pytest.raises(TimeoutError, match="after content"):
        next(iterator)
    assert calls == [{"model": "primary"}]


def test_moa_fallback_retrims_for_destination_and_keeps_liveness(moa_transport, monkeypatch):
    from agent import moa_loop as moa
    _cfg, calls, progress, agent = moa_transport
    checked = []
    def context(slot, runtime, cache):
        checked.append(slot["model"])
        return 100000 if slot["model"] == "primary" else 1500
    monkeypatch.setattr(moa, "_reference_context_length", context)
    messages = [{"role": "user", "content": "old " * 2500}, {"role": "assistant", "content": "old answer " * 2500},
                {"role": "user", "content": "latest"}]
    _, output, acct = moa._run_reference({**PRIMARY, "fallback_models": [BACKUP]}, messages, max_tokens=64, agent=agent)
    assert output == "backup executed"
    assert "primary" in checked and "backup" in checked
    assert len(str(calls[-1]["messages"])) < len(str(calls[0]["messages"]))
    assert acct.messages == calls[-1]["messages"]
    assert calls[-1]["messages"][-1]["content"] == "latest"
    assert progress


@pytest.mark.parametrize("reference", ["p", "default", "attended"])
def test_raw_mapping_and_builtin_wrappers_share_fallback_optout(reference):
    from hermes_cli.model_preset_runtime import resolve_model_ref
    cfg = {"model_presets": {"p": PRIMARY}, "model": {"default": "@p", "attended": {"model": "@p"}}}
    assert resolve_model_ref("@" + reference, cfg).fallbacks
    assert resolve_model_ref({"model": "@" + reference, "fallbacks": []}, cfg).fallbacks == ()
    cfg["model"]["attended"]["fallbacks"] = []
    assert resolve_model_ref("@attended", cfg).fallbacks == ()


def test_auxiliary_per_call_chain_inherits_only_when_none(moa_transport, monkeypatch):
    from agent import auxiliary_client as aux
    cfg, calls, _progress, _agent = moa_transport
    cfg["auxiliary"]["moa_reference"] = {"fallback_chain": [BACKUP]}
    from pathlib import Path
    import os
    (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    kwargs = {"task": "moa_reference", "provider": "openrouter", "model": "primary",
              "messages": [{"role": "user", "content": "run"}]}
    route = {}
    assert aux.call_llm(**kwargs, fallback_chain=None, route_info=route).choices[0].message.content == "backup executed"
    assert {key: route[key] for key in ("provider", "model")} == {"provider": "openrouter", "model": "backup"}
    calls.clear()
    with pytest.raises(TimeoutError):
        aux.call_llm(**kwargs, fallback_chain=[])
    assert [call["model"] for call in calls] == ["primary"]
    calls.clear()
    assert aux.call_llm(**kwargs).choices[0].message.content == "backup executed"
    assert [call["model"] for call in calls] == ["primary", "backup"]


@pytest.mark.parametrize("consumer", ["reference", "aggregator", "streaming"])
@pytest.mark.parametrize("provider", ["openrouter", "anthropic"])
@pytest.mark.parametrize("policy", ["empty", "exhausted", "declared", "inherited"])
def test_moa_unavailable_primary_obeys_policy_and_prepares_destination(moa_transport, monkeypatch, tmp_path, consumer, provider, policy):
    from agent import auxiliary_client as aux, moa_loop as moa
    cfg, calls, progress, agent = moa_transport
    visited, planned, cache_routes = [], [], []
    client = SimpleNamespace(base_url="https://openrouter.ai/api/v1", api_key="fixture")
    def create(**kw):
        calls.append(kw)
        aux._notify_aux_progress()
        return SimpleNamespace(model=kw["model"], choices=[SimpleNamespace(
            message=SimpleNamespace(role="assistant", content="executed", tool_calls=None), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7))
    client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    def cached(provider, model=None, **kw):
        visited.append((provider, model))
        return (client, "unrelated") if provider == "auto" else (None, None)
    def resolve(provider, model=None, **kw):
        visited.append((provider, model))
        return (None, None) if model == "unusable" else (client, model)
    monkeypatch.setattr(aux, "_get_cached_client", cached)
    monkeypatch.setattr(aux, "resolve_provider_client", resolve)
    monkeypatch.setattr(moa, "_slot_runtime", lambda slot: {"provider": slot["provider"], "model": slot["model"]})
    monkeypatch.setattr(moa, "_reference_context_length", lambda slot, *a: 100000 if slot["model"] == "primary" else 1500)
    replan = aux._replan_synchronous_cache_sections
    def cache(messages, tools, *, destination):
        planned.append(destination)
        return replan(messages, tools, destination=destination)
    monkeypatch.setattr(aux, "_replan_synchronous_cache_sections", cache)
    cache_aggregator = moa.MoAChatCompletions._plan_aggregator_cache
    def aggregator_cache(self, messages, tools, guidance, runtime):
        cache_routes.append(dict(runtime))
        return cache_aggregator(self, messages, tools, guidance, runtime)
    monkeypatch.setattr(moa.MoAChatCompletions, "_plan_aggregator_cache", aggregator_cache)
    chain = [{"provider": "custom:missing", "model": "unusable"}]
    if policy != "exhausted":
        chain.append(BACKUP)
    task = "moa_reference" if consumer == "reference" else "moa_aggregator"
    cfg["auxiliary"][task] = {"fallback_chain": chain}
    write_config(tmp_path, monkeypatch, cfg)
    slot = {"provider": provider, "model": "primary", "reasoning_effort": "high"}
    if policy != "inherited":
        slot["fallback_models"] = [] if policy == "empty" else chain
    messages = [{"role": "user", "content": "old " * 2500}, {"role": "assistant", "content": "old answer " * 2500},
                {"role": "user", "content": "latest"}]
    # Inherited OpenRouter discovery is intentionally preserved. Declared policy is never discovery.
    expected = "unrelated" if policy == "inherited" and provider == "openrouter" else "backup"
    if consumer == "reference":
        _, text, acct = moa._run_reference(slot, messages, max_tokens=64, agent=agent)
        if policy in {"empty", "exhausted"}:
            assert text.startswith("[failed:")
        else:
            assert text == "executed"
            assert (acct.model, acct.provider) == ("primary" if expected == "unrelated" else expected, "openrouter")
            assert acct.usage.input_tokens == 5
            assert acct.messages == calls[-1]["messages"]
    else:
        facade = moa.MoAChatCompletions("default", agent=agent)
        def execute():
            result = facade._call_prepared_aggregator({"aggregator": slot, "messages": messages,
                "guidance": None, "aggregator_temperature": None}, {"stream": consumer == "streaming", "max_tokens": 64})
            assert calls  # provider dispatch remains eager
            return "".join(str(c.choices[0].delta.content or "") for c in result) if consumer == "streaming" else moa._extract_text(result)
        if policy in {"empty", "exhausted"}:
            with pytest.raises(aux.AuxiliaryClientUnavailable):
                execute()
        else:
            assert execute() == "executed"
            assert facade.last_aggregator_slot["model"] == ("primary" if expected == "unrelated" else expected)
            assert facade.last_aggregator_slot["provider"] == "openrouter"
            if expected == "backup":
                assert facade.last_aggregator_slot["reasoning_effort"] == "none"
    assert visited[0] == (provider, "primary")
    if policy == "empty":
        assert visited == [(provider, "primary")]
        assert not calls and not planned
    elif policy == "exhausted":
        assert visited == [(provider, "primary"), ("custom:missing", "unusable")]
        assert not calls and not planned
    elif expected == "unrelated":
        assert visited == [(provider, "primary"), ("auto", None)]
        assert calls[-1]["model"] == "unrelated"
    elif expected == "backup":
        assert visited == [(provider, "primary"), ("custom:missing", "unusable"), ("openrouter", "backup")]
        assert planned[-1].model == "backup"
        assert calls[-1]["extra_body"]["reasoning"] == {"enabled": False}
        if consumer == "reference":
            assert len(str(calls[-1]["messages"])) < len(str(messages))
        else:
            assert calls[-1]["messages"] == messages
            assert cache_routes[-1] == {"provider": "openrouter", "model": "backup",
                "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions"}
        assert "latest" in str(calls[-1]["messages"][-1])
        assert progress or consumer != "reference"
