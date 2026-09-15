"""Configured reference chains remain bounded by frozen route authority."""
import copy
import json
from types import SimpleNamespace

import pytest
import yaml


@pytest.mark.parametrize("status,body,uses_fallback", [
    (429, "usage exhausted", True), (503, "unavailable", True),
    (404, "model_not_found", True), (402, "quota exhausted", True),
    (401, "unauthorized", False), (403, "forbidden", False),
    (400, "invalid parameter", False),
])
def test_frozen_reference_chain(tmp_path, monkeypatch, status, body, uses_fallback):
    from agent import moa_loop
    from hermes_cli import runtime_provider

    grok = {"provider": "xai-oauth", "model": "grok-4.6"}
    opus = {"provider": "anthropic", "model": "claude-opus-4-8", "reasoning_effort": "high"}
    fable = {"provider": "anthropic", "model": "claude-fable-5-1", "fallback_models": [opus]}
    sol = {"provider": "custom", "model": "gpt-5.6-sol"}
    config = {"moa": {"reference_models": [grok, fable], "aggregator": sol}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Fake the credential boundary, not config normalization/freeze/resume,
    # threaded fan-out, guidance, aggregation or accounting.
    def resolve(*, requested, target_model):
        return dict(provider=requested, model=target_model, base_url="https://fixture.test/v1",
                    api_key="fixture-secret", api_mode="chat_completions")
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve)
    snapshot = moa_loop.snapshot_moa_preset("default")
    metadata = snapshot.metadata()
    assert "fixture-secret" not in json.dumps(metadata)
    restored = moa_loop.restore_moa_preset(metadata)
    assert restored.fingerprint == snapshot.fingerprint
    bad = copy.deepcopy(metadata)
    bad["preset_snapshot"]["reference_models"][1]["fallback_models"][0]["model"] = "unapproved"
    with pytest.raises(ValueError, match="fingerprint"):
        moa_loop.restore_moa_preset(bad)
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: {
        **resolve(**kw), "api_key": "changed" if kw["target_model"] == opus["model"] else "fixture-secret"})
    with pytest.raises(ValueError, match="authority"):
        moa_loop.restore_moa_preset(metadata)

    calls, events = [], []
    class Failure(Exception):
        status_code = status
    def call(**kwargs):
        calls.append(kwargs)
        assert kwargs["strict_route"] is True
        if kwargs["model"] == fable["model"]:
            raise Failure(body)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))], usage=None)
    monkeypatch.setattr(moa_loop, "call_llm", call)
    agent = SimpleNamespace(_moa_preset_snapshot=restored)
    client = moa_loop.MoAClient("default", reference_callback=lambda event, **kw: events.append((event, kw)), agent=agent)
    client.chat.completions.create(messages=[{"role": "user", "content": "Say OK"}], model="default")
    models = [c["model"] for c in calls]
    assert models.count(grok["model"]) == models.count(sol["model"]) == 1
    assert models.count(fable["model"]) == 1
    assert models.count(opus["model"]) == int(uses_fallback)
    outputs = client.chat.completions._ref_cache_outputs
    label, text, acct = outputs[1]
    assert fable["model"] in label
    guidance = str(calls[-1]["messages"])
    if uses_fallback:
        assert opus["model"] in label and "->" in label
        assert acct.model == opus["model"] and acct.rerouted
        assert "rerouted" in guidance
    else:
        assert text.startswith("[failed:") and "unavailable" in guidance
    assert any(e == "moa.reference" and kw["label"] == label for e, kw in events)


@pytest.mark.parametrize("status,exhausted,stream", [(429, False, False), (503, True, False), (401, False, False), (429, False, True)])
def test_aggregator_failure_boundary(monkeypatch, status, exhausted, stream):
    from agent import moa_loop
    from hermes_cli.moa_config import normalize_moa_config
    primary = {"provider": "custom", "model": "primary", "fallback_models": [{"provider": "custom", "model": "secondary"}]}
    preset = normalize_moa_config({"reference_models": [{"provider": "custom", "model": "advisor"}], "aggregator": primary})["presets"]["default"]
    monkeypatch.setattr(moa_loop, "_resolve_preset_cached", lambda name: (preset, {}))
    monkeypatch.setattr(moa_loop, "_slot_runtime", lambda slot: {"provider": slot["provider"], "model": slot["model"]})
    calls = []
    class Failure(Exception):
        status_code = status
    def call(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "advisor":
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="advice"))], usage=None)
        def chunks():
            if kwargs["model"] == "primary" or exhausted:
                raise Failure("fixture")
            yield SimpleNamespace(choices=[])
        if stream:
            return chunks()
        if kwargs["model"] == "primary" or exhausted:
            raise Failure("fixture")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))], usage=None)
    monkeypatch.setattr(moa_loop, "call_llm", call)
    client = moa_loop.MoAClient("default")
    if exhausted or status == 401:
        with pytest.raises((RuntimeError, Failure)):
            client.chat.completions.create(messages=[{"role": "user", "content": "question"}], stream=stream)
    else:
        response = client.chat.completions.create(messages=[{"role": "user", "content": "question"}], stream=stream)
        if stream:
            assert len(list(response)) == 1
        assert client.last_aggregator_slot["model"] == "secondary"
    assert calls == ["advisor", "primary"] + ([] if status == 401 else ["secondary"])


@pytest.mark.parametrize("chain", [None, {}, "opus", [None], [{"provider": "anthropic"}],
    [{"provider": "moa", "model": "default"}], [{"provider": "auto", "model": "opus"}],
    [{"provider": "anthropic", "model": "opus", "fallback_models": []}],
    [{"provider": "anthropic", "model": "opus", "reasoning_effort": "bogus"}],
    [{"provider": "anthropic", "model": "opus", "api_key": "secret"}],
])
def test_invalid_reference_chains_fail_closed(chain):
    from hermes_cli.moa_config import normalize_moa_config, validate_moa_payload
    config = {"reference_models": [{"provider": "anthropic", "model": "fable", "fallback_models": chain}],
              "aggregator": {"provider": "custom", "model": "sol"}}
    assert validate_moa_payload(config)
    with pytest.raises(ValueError, match="fallback_models"):
        normalize_moa_config(config)


@pytest.mark.parametrize("consume", [0, 1, 2, 3])
@pytest.mark.parametrize("fails", [False, True])
def test_prefetched_aggregator_closes_transport_before_or_during_consumption(
    tmp_path, monkeypatch, consume, fails
):
    """A caller may abandon a returned stream before reading its prefetched chunk."""
    import threading
    from agent import moa_loop

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"moa": {
        "reference_models": [{"provider": "custom", "model": "advisor"}],
        "aggregator": {"provider": "custom", "model": "primary",
                       "fallback_models": [{"provider": "custom", "model": "secondary"}]},
    }}))
    monkeypatch.setattr(moa_loop, "_slot_runtime", lambda slot: {
        "provider": slot["provider"], "model": slot["model"]})
    permit = threading.BoundedSemaphore(1)
    chunks = [object(), object()]
    closed, calls, sources = [], [], []

    def transport():
        assert permit.acquire(blocking=False)
        try:
            yield chunks[0]
            yield chunks[1]
            if fails:
                raise ConnectionError("midstream failure must not replay")
        finally:
            closed.append(True)
            permit.release()

    def call(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "advisor":
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="advice"))], usage=None)
        source = transport()
        sources.append(source)  # SDK may retain its stream independently of the consumer
        return source

    monkeypatch.setattr(moa_loop, "call_llm", call)
    client = moa_loop.MoAClient("default")
    stream = client.chat.completions.create(
        messages=[{"role": "user", "content": "question"}], stream=True)
    assert not permit.acquire(blocking=False)  # real aggregator prefetch owns the permit
    try:
        assert iter(stream) is stream
        for index in range(min(consume, 2)):
            assert next(stream) is chunks[index]
        if consume == 3:
            with pytest.raises(ConnectionError if fails else StopIteration):
                next(stream)
            assert closed == [True]
        stream.close()
        stream.close()
        assert closed == [True]
        assert permit.acquire(blocking=False)
        permit.release()
        with pytest.raises(StopIteration):
            next(stream)
        assert calls == ["advisor", "primary"]
    finally:
        stream.close()
        for source in sources:
            source.close()


def test_prefetched_stream_closes_distinct_source_once_even_when_close_raises():
    from agent.moa_fallback import prefetched_stream

    class Source:
        close_count = 0

        def close(self):
            self.close_count += 1
            raise OSError("fixture close error")

    source = Source()
    stream = prefetched_stream(None, iter(["tail"]), source)
    with pytest.raises(OSError, match="fixture close error"):
        stream.close()
    stream.close()
    assert source.close_count == 1
    with pytest.raises(StopIteration):
        next(stream)


def test_prefetched_stream_closes_separate_live_iterator_and_source():
    from agent.moa_fallback import prefetched_stream

    released = []

    def chunks():
        try:
            yield "first"
            yield "tail"
        finally:
            released.append("iterator")

    iterator = chunks()
    source = SimpleNamespace(close=lambda: released.append("source"))
    stream = prefetched_stream(next(iterator), iterator, source)
    stream.close()
    stream.close()
    assert released == ["iterator", "source"]
