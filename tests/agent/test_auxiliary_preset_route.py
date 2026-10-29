"""A resolved auxiliary fallback carries its preset effort without config rereads."""
from types import SimpleNamespace
from unittest.mock import Mock


def test_main_fallback_carries_resolved_entry_to_request(monkeypatch):
    import agent.auxiliary_client as aux
    from hermes_cli import config
    import hermes_cli.fallback_config as fallback

    entry = {"provider": "openrouter", "model": "fallback-model", "reasoning_effort": "high"}
    read = Mock(return_value={"fallback_models": [entry]})
    monkeypatch.setattr(config, "load_config_readonly", read)
    monkeypatch.setattr(fallback, "get_fallback_chain", lambda cfg: cfg["fallback_models"])
    monkeypatch.setattr(aux, "_resolve_fallback_entry", lambda *a: (SimpleNamespace(), "fallback-model"))
    monkeypatch.setattr(aux, "_context_too_small", lambda *a, **kw: None)
    client, model, label = aux._try_main_fallback_chain(
        "helper", reason="failure", failed_provider="openai", failed_model="failed-model")
    assert read.call_count == 1
    assert client is not None
    assert client._hermes_fallback_entry == entry
    # An invalid config edit after selection must not lose this candidate's effort.
    read.side_effect = AssertionError("request planning re-read config")
    monkeypatch.setattr(aux, "_fallback_entry_timeout", lambda *a: None)
    monkeypatch.setattr(aux, "_fallback_destination", lambda *a: SimpleNamespace(provider="openrouter"))
    monkeypatch.setattr(aux, "_fallback_request_kwargs", lambda *a, **kw: kw)
    _, kwargs, _ = aux._plan_fallback_candidate(
        client, model, label, task="helper", effective_timeout=10, apply_fast_lane=False)
    assert kwargs["fallback_entry"]["reasoning_effort"] == "high"
    assert read.call_count == 1
