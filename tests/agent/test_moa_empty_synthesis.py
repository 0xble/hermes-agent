"""One-shot synthesis must not discard usable, privacy-filtered advice."""
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.parametrize("synthesis", ["", " \n\t", "usable synthesis"])
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("privacy", ["", "full"])
def test_one_shot_preserves_guidance_and_route(monkeypatch, synthesis, fallback, privacy):
    from agent import moa_loop
    from hermes_cli import config

    accounting = moa_loop._RefAccounting(usage=None, rerouted=True)
    refs = [("custom:advisor", "Check the logs; contact fixture.person@example.com", accounting),
            ("custom:unavailable", "[failed: private error detail]", None)]
    monkeypatch.setattr(moa_loop, "_run_references_parallel", lambda *a, **kw: refs)
    monkeypatch.setattr(config, "load_config", lambda: {"moa": {"privacy_filter": privacy}})
    monkeypatch.setattr(moa_loop, "_slot_runtime", lambda slot: {
        "provider": slot["provider"], "model": slot["model"],
        "api_key": "fixture", "base_url": "https://fixture.invalid/v1",
        "api_mode": "chat_completions",
    })
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=synthesis))])

    monkeypatch.setattr(moa_loop, "call_llm", transport)
    aggregator: dict[str, Any] = {"provider": "custom", "model": "aggregator"}
    if fallback:
        aggregator["fallback_models"] = [{"provider": "custom", "model": "fallback"}]
    result = moa_loop.aggregate_moa_context(
        user_prompt="diagnose", api_messages=[],
        reference_models=[{"provider": "custom", "model": "advisor"}], aggregator=aggregator,
    )
    assert len(calls) == 1
    assert calls[0]["model"] == "aggregator" and calls[0]["strict_route"] is True
    assert "Aggregator: custom:aggregator" in result
    assert "private error detail" not in result
    if synthesis.strip():
        assert result.endswith(synthesis.strip())
        assert "Check the logs" not in result
    else:
        assert "Check the logs" in result
        assert "Reference models unavailable: custom:unavailable" in result
        assert "Reference models rerouted: custom:advisor" in result
    if privacy == "full":
        assert "fixture.person@example.com" not in result
        assert "fixture.person@example.com" not in calls[0]["messages"][0]["content"]
    elif not synthesis.strip():
        assert "fixture.person@example.com" in result
    # Redaction is per-call, not a mutation of cached reference outputs.
    assert "fixture.person@example.com" in refs[0][1]
    assert refs[0][2] is accounting


@pytest.mark.parametrize("all_failed", [False, True])
def test_one_shot_does_not_mask_failed_references_or_auth(monkeypatch, all_failed):
    from agent import moa_loop
    from hermes_cli import config

    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(moa_loop, "_run_references_parallel", lambda *a, **kw: [
        ("custom:advisor", "[failed: unavailable]" if all_failed else "usable guidance", None),
    ])
    monkeypatch.setattr(moa_loop, "_slot_runtime", lambda slot: {"model": slot["model"]})
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        raise PermissionError("authorization denied")

    monkeypatch.setattr(moa_loop, "call_llm", transport)
    kwargs: dict[str, Any] = dict(user_prompt="diagnose", api_messages=[],
                  reference_models=[{"provider": "custom", "model": "advisor"}],
                  aggregator={"provider": "custom", "model": "aggregator", "fallback_models": [
                      {"provider": "custom", "model": "fallback"}]})
    if all_failed:
        assert "all reference models failed" in moa_loop.aggregate_moa_context(**kwargs)
        assert calls == []
    else:
        with pytest.raises(PermissionError, match="authorization denied"):
            moa_loop.aggregate_moa_context(**kwargs)
        assert len(calls) == 1
