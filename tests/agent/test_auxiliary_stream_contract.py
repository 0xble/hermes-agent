"""Streaming response shape and permit lifetime at the shared auxiliary boundary."""
import gc
import weakref
from types import SimpleNamespace
from unittest.mock import Mock

import hermes_yaml as yaml
import pytest


@pytest.fixture
def transport(tmp_path, monkeypatch):
    from agent import auxiliary_client as aux

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "openrouter", "default": "primary"},
        "auxiliary": {"transient_retries": 0, "moa_aggregator": {"max_concurrency": 1}},
    }))
    create = Mock()
    client = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1", api_key="fixture",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    aux._reset_aux_semaphores()
    monkeypatch.setattr(aux, "_get_cached_client", lambda provider, model=None, **kw: (client, model))
    monkeypatch.setattr(aux, "resolve_provider_client", lambda provider, model=None, **kw: (client, model))
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda *a, **kw: False)
    monkeypatch.setattr(aux, "_mark_provider_unhealthy", lambda *a, **kw: None)
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **kw: False)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **kw: None)
    yield aux, create
    aux._reset_aux_semaphores()


class _IterableCompleted(SimpleNamespace):
    def __iter__(self):
        raise AssertionError("completed SDK fields must not be iterated as chunks")


@pytest.mark.parametrize("completed_type", [SimpleNamespace, _IterableCompleted])
@pytest.mark.parametrize("recover_on_send", [False, True])
def test_completed_response_keeps_identity_and_releases_permit(transport, completed_type, recover_on_send):
    aux, create = transport
    completed = completed_type(
        model="backup" if recover_on_send else "primary",
        choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1, total_tokens=4),
    )
    create.side_effect = [TimeoutError("primary unavailable"), completed] if recover_on_send else [completed]
    response = aux.call_llm(
        "moa_aggregator", provider="openrouter", model="primary", messages=[{"role": "user", "content": "q"}],
        stream=True, fallback_chain=[{"provider": "openrouter", "model": "backup"}],
    )
    assert response is completed
    assert response.usage.total_tokens == 4
    permit = aux._acquire_sync_aux_semaphore("moa_aggregator")
    assert permit.acquire(blocking=False)
    permit.release()
    assert create.call_count == (2 if recover_on_send else 1)


_STREAM_ENDINGS = ["close_unread", "exhaust", "error_unread", "error_emitted", "close_emitted", "close_error", "recover_unread", "abandon_unread", "abandon_emitted"]


@pytest.mark.parametrize("ending", _STREAM_ENDINGS)
def test_stream_dispatch_scope_recovery_and_permit_lifetime(transport, ending):
    _assert_stream_contract(transport, ending)


@pytest.mark.parametrize("ending", _STREAM_ENDINGS)
def test_unavailable_primary_preserves_stream_contract(transport, ending):
    _assert_stream_contract(transport, ending, unavailable_primary=True)


def _assert_stream_contract(transport, ending, unavailable_primary=False):
    from agent.auxiliary_call_policy import call_fallback_chain

    aux, create = transport
    observed = []
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))])
    backup = {"provider": "openrouter", "model": "backup"}
    chain = [backup] if ending == "recover_unread" else []
    if unavailable_primary:
        # Fixture teardown restores its original monkeypatch, including on eager failure.
        aux._get_cached_client = lambda *a, **kw: (None, None)
        first = {"provider": "openrouter", "model": "first-backup"}
        chain.insert(0, first)
    runtime = {"provider": "openrouter", "model": "runtime"}
    request_id = None

    class Stream:
        closed = pulls = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.pulls += 1
            observed.append((call_fallback_chain("moa_aggregator", None),
                             aux._RELAY_AUX_CALL_CONTEXT.get()["request_id"], aux._runtime_main_value("model")))
            if ending in {"error_unread", "recover_unread"} or (ending == "error_emitted" and self.pulls > 1):
                raise TimeoutError("stream failed")
            if self.pulls > 1:
                raise StopIteration
            return chunk

        def close(self):
            self.closed += 1
            assert call_fallback_chain("moa_aggregator", None) == chain
            assert aux._RELAY_AUX_CALL_CONTEXT.get()["request_id"] == request_id
            if ending == "close_error":
                raise RuntimeError("close failed")

    raw = Stream()

    def send(**kwargs):
        nonlocal request_id
        if kwargs["model"] == "backup":
            assert aux._RELAY_AUX_CALL_CONTEXT.get()["request_id"] == request_id
            return SimpleNamespace(model="backup", choices=[SimpleNamespace(
                message=SimpleNamespace(content="recovered", tool_calls=None), finish_reason="stop")])
        request_id = aux._RELAY_AUX_CALL_CONTEXT.get()["request_id"]
        return raw

    create.side_effect = send
    stream = aux.call_llm(
        "moa_aggregator", provider="openrouter", model="primary", messages=[{"role": "user", "content": "q"}],
        stream=True, stream_options={"include_usage": True}, main_runtime=runtime, fallback_chain=chain,
    )
    assert create.call_count == 1
    assert create.call_args.kwargs["stream_options"] == {"include_usage": True}
    permit = aux._acquire_sync_aux_semaphore("moa_aggregator")
    assert not permit.acquire(blocking=False)
    try:
        if ending in {"exhaust", "error_emitted", "close_emitted", "abandon_emitted"}:
            assert next(stream) is chunk
        assert call_fallback_chain("moa_aggregator", None) is None
        assert aux._RELAY_AUX_CALL_CONTEXT.get() is None
        assert aux._RUNTIME_MAIN_CONTEXT.get() is None
        if ending in {"error_unread", "error_emitted"}:
            with pytest.raises(TimeoutError, match="stream failed"):
                next(stream)
        elif ending == "exhaust":
            assert list(stream) == []
        elif ending == "recover_unread":
            assert [c.choices[0].delta.content for c in stream] == ["recovered"]
        elif ending == "close_error":
            with pytest.raises(RuntimeError, match="close failed"):
                stream.close()
        elif ending.startswith("abandon"):
            ref = weakref.ref(stream)
            stream = None
            gc.collect()
            assert ref() is None
        else:
            stream.close()
    finally:
        if stream is not None:
            stream.close()
    assert raw.closed == 1
    assert raw.pulls == (0 if ending in {"close_unread", "close_error", "abandon_unread"} else 2 if ending in {"exhaust", "error_emitted"} else 1)
    assert all(row == (chain, request_id, "runtime") for row in observed)
    assert create.call_count == (2 if ending == "recover_unread" else 1)
    assert call_fallback_chain("moa_aggregator", None) is None
    assert aux._RELAY_AUX_CALL_CONTEXT.get() is None
    assert permit.acquire(blocking=False)
    permit.release()
    if stream is not None:
        with pytest.raises(StopIteration):
            next(stream)


@pytest.mark.parametrize("async_mode", [False, True])
def test_unavailable_selection_is_local_and_cached_primary_is_not_a_fallback(transport, tmp_path, monkeypatch, async_mode):
    import asyncio
    from agent.auxiliary_call_policy import call_fallback_chain

    aux, create = transport
    client, _ = aux._get_cached_client("openrouter", "unused")
    backup = {"provider": "openrouter", "model": "backup", "reasoning_effort": "none"}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "auxiliary": {"moa_aggregator": {"fallback_chain": [backup]}},
    }))
    completed = _IterableCompleted(choices=[SimpleNamespace(
        message=SimpleNamespace(content="done", tool_calls=None), finish_reason="stop")])
    seen, transforms = [], []
    def dispatch(**kw):
        seen.append(kw)
        return completed
    create.side_effect = dispatch
    if async_mode:
        async def async_dispatch(**kw):
            return dispatch(**kw)
        client.chat.completions.create = async_dispatch
        monkeypatch.setattr(aux, "_to_async_client", lambda client, model, **kw: (client, model))
    primary_available = False
    monkeypatch.setattr(aux, "_get_cached_client", lambda provider, model=None, **kw:
                        (client, model) if primary_available else (None, None))
    def transform(destination, messages, tools, reasoning, entry):
        transforms.append(destination.model)
        return messages, tools, reasoning
    def invoke(**policy):
        route = {}
        kwargs = dict(task="moa_aggregator", provider="anthropic", model="primary",
                      messages=[{"role": "user", "content": "q"}], route_info=route,
                      reasoning_config={"enabled": True, "effort": "high"})
        if async_mode:
            result = asyncio.run(aux.async_call_llm(**kwargs))
        else:
            result = aux.call_llm(**kwargs, stream=True, **policy)
        assert result is completed
        return route
    assert invoke(**({} if async_mode else {"fallback_chain": [backup], "fallback_request_transform": transform})) == {
        "provider": "openrouter", "model": "backup"}
    assert seen[-1]["extra_body"]["reasoning"]["enabled"] is False
    assert client._hermes_fallback_destination.model == "backup"
    primary_available = True
    assert invoke(**({} if async_mode else {"fallback_request_transform": transform})) == {
        "provider": "anthropic", "model": "primary"}
    assert seen[-1]["model"] == "primary"
    assert seen[-1]["extra_body"]["reasoning"]["effort"] == "high"
    assert transforms == ([] if async_mode else ["backup"])
    assert call_fallback_chain("moa_aggregator", None) is None
    if not async_mode:
        primary_available = False
        with pytest.raises(aux.AuxiliaryClientUnavailable):
            invoke(fallback_chain=[])
        assert invoke() == {"provider": "openrouter", "model": "backup"}
        assert transforms == ["backup"]
