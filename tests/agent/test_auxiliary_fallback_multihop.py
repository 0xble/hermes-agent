"""Auxiliary ``provider: auto`` fallback walk is multi-hop (#106367).

When a ``fallback_providers`` candidate itself fails with a quota/rate-limit/payment/capacity
error, the walk must advance to the next configured entry instead of letting the candidate's
exception escape after a single hop. Every lane is attempted at most once; when the whole chain
is exhausted the primary (narrowed) error still surfaces as a controlled failure. Sync and async
share the walk. A quarantined candidate is hidden for a TTL that matches its failure class: seconds
for a transient 429 / dropped connection, the long payment hold only for depleted credit.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import auxiliary_client as ac
from agent.auxiliary_client import async_call_llm, call_llm


@pytest.fixture(autouse=True)
def _fresh_unhealthy_cache():
    ac._reset_aux_unhealthy_cache()
    yield
    ac._reset_aux_unhealthy_cache()


def _quota_429(lane: str) -> Exception:
    exc = Exception(
        f"Error code: 429 - {{'error': {{'message': 'Weekly usage limit reached', "
        f"'type': 'usage_limit_reached', 'code': 'rate_limit_exceeded', 'lane': '{lane}'}}}}"
    )
    exc.status_code = 429
    return exc


def _plain_429(lane: str) -> Exception:
    exc = Exception(f"Error code: 429 - {{'error': {{'message': 'Rate limit exceeded, retry in 20s', 'lane': '{lane}'}}}}")
    exc.status_code = 429
    return exc


def _payment_402(lane: str) -> Exception:
    exc = Exception(f"Error code: 402 - {{'error': {{'message': 'Insufficient credits', 'lane': '{lane}'}}}}")
    exc.status_code = 402
    return exc


def _quarantine_remaining(base_url: str, model: str) -> float:
    key = ac._unhealthy_cache_key("custom", base_url, model)
    return ac._aux_unhealthy_until[key] - time.time()


def _client(base_url: str, create):
    client = MagicMock()
    client.base_url = base_url
    client.chat.completions.create = create
    return client


def _ok_response(text: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    resp.choices[0].message.tool_calls = None
    return resp


def _walk_patches(primary, main_chain_selections):
    """``provider: auto`` primary; per-task chain empty; main ``fallback_providers`` hands out the
    given (client, model, label) tuples in order and then reports exhaustion; discovery is empty."""
    return (
        patch("agent.auxiliary_client._resolve_task_provider_model",
              return_value=("auto", None, None, None, None)),
        patch("agent.auxiliary_client._get_cached_client", return_value=(primary, "modelA")),
        patch("agent.auxiliary_client._try_configured_fallback_chain", return_value=(None, None, "")),
        patch("agent.auxiliary_client._try_main_fallback_chain",
              side_effect=list(main_chain_selections) + [(None, None, "")]),
        patch("agent.auxiliary_client._try_payment_fallback", return_value=(None, None, "")),
    )


def test_sync_walk_advances_past_quota_limited_candidate_to_next_configured_entry():
    """T3: primary 429 → fallback_providers[0] 429 → fallback_providers[1] serves."""
    primary = _client("http://127.0.0.1:1/v1", MagicMock(side_effect=_quota_429("A")))
    lane_b = _client("http://127.0.0.1:2/v1", MagicMock(side_effect=_quota_429("B")))
    lane_c = _client("http://127.0.0.1:3/v1", MagicMock(return_value=_ok_response("OK")))

    patches = _walk_patches(primary, [(lane_b, "modelB", "custom"), (lane_c, "modelC", "custom")])
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = call_llm(task="title_generation", messages=[{"role": "user", "content": "Reply OK"}])

    assert result.choices[0].message.content == "OK"
    assert lane_b.chat.completions.create.call_count == 1
    assert lane_c.chat.completions.create.call_count == 1
    # The quota-limited candidate is really quarantined (per endpoint) so the ordered re-walk skips it.
    assert ac._is_provider_unhealthy("custom", "http://127.0.0.1:2/v1", "modelB")
    assert not ac._is_provider_unhealthy("custom", "http://127.0.0.1:3/v1", "modelC")


def test_sync_walk_advances_when_route_health_persist_fails(monkeypatch):
    """A full state directory must not stop the in-process fallback walk."""
    primary = _client("http://127.0.0.1:1/v1", MagicMock(side_effect=_quota_429("A")))
    lane_b = _client("http://127.0.0.1:2/v1", MagicMock(side_effect=_quota_429("B")))
    lane_c = _client("http://127.0.0.1:3/v1", MagicMock(return_value=_ok_response("OK")))
    monkeypatch.setattr(ac, "_mark_auxiliary_health", MagicMock(side_effect=OSError("disk full")))

    patches = _walk_patches(primary, [(lane_b, "modelB", "custom"), (lane_c, "modelC", "custom")])
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = call_llm(task="title_generation", messages=[{"role": "user", "content": "Reply OK"}])

    assert result.choices[0].message.content == "OK"
    assert lane_b.chat.completions.create.call_count == 1
    assert lane_c.chat.completions.create.call_count == 1


@pytest.mark.asyncio
async def test_async_walk_exhausts_every_configured_lane_once_then_raises_primary_error():
    """T4: primary 429 → fallback[0] 429 → fallback[1] 429 → controlled exhaustion; each lane once;
    the primary's own error is what surfaces."""
    primary = _client("http://127.0.0.1:1/v1", AsyncMock(side_effect=_quota_429("A")))
    lane_b = _client("http://127.0.0.1:2/v1", AsyncMock(side_effect=_quota_429("B")))
    lane_c = _client("http://127.0.0.1:3/v1", AsyncMock(side_effect=_quota_429("C")))

    patches = _walk_patches(primary, [(lane_b, "modelB", "custom"), (lane_c, "modelC", "custom")])
    with patches[0], patches[1], patches[2], patches[3] as main_chain, patches[4] as discovery, \
            patch("agent.auxiliary_client._to_async_client", side_effect=lambda c, m, **kw: (c, m)):
        with pytest.raises(Exception, match="'lane': 'A'"):
            await async_call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])

    assert lane_b.chat.completions.create.call_count == 1
    assert lane_c.chat.completions.create.call_count == 1
    # Walk stopped once the configured chain reported exhaustion — no lane was re-tried.
    assert main_chain.call_count == 3
    # Exhaustion is controlled: discovery was consulted and found nothing, no fourth lane appended.
    assert discovery.call_count >= 1


def test_candidate_quarantine_ttl_is_short_for_transient_429_and_long_for_payment(caplog):
    """A per-minute 429 on a candidate hides the lane for seconds, not the 10-minute payment hold,
    and the warning names the real class; a 402 candidate still gets the long hold."""
    caplog.set_level("WARNING", logger="agent.auxiliary_client")
    primary = _client("http://127.0.0.1:1/v1", MagicMock(side_effect=_plain_429("A")))
    lane_b = _client("http://127.0.0.1:2/v1", MagicMock(side_effect=_plain_429("B")))
    lane_c = _client("http://127.0.0.1:3/v1", MagicMock(side_effect=_payment_402("C")))
    lane_d = _client("http://127.0.0.1:4/v1", MagicMock(return_value=_ok_response("OK")))

    patches = _walk_patches(
        primary, [(lane_b, "modelB", "custom"), (lane_c, "modelC", "custom"), (lane_d, "modelD", "custom")])
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = call_llm(task="title_generation", messages=[{"role": "user", "content": "Reply OK"}])

    assert result.choices[0].message.content == "OK"
    assert 0 < _quarantine_remaining("http://127.0.0.1:2/v1", "modelB") <= 60
    assert _quarantine_remaining("http://127.0.0.1:3/v1", "modelC") > 60
    marks = [r.getMessage() for r in caplog.records if "marking local/custom unhealthy" in r.getMessage()]
    assert marks and "payment" not in marks[0] and "rate limit" in marks[0]


def test_discovery_rewalk_uses_the_resolved_model_when_openrouter_is_quarantined(monkeypatch):
    """A failed OpenRouter hop must not be selected again just because its model was resolved late."""
    openrouter = _client("https://openrouter.ai/api/v1", MagicMock())
    nous = _client("https://inference-api.nousresearch.com/v1", MagicMock())
    monkeypatch.setattr(ac, "_read_main_provider", lambda: "auto")
    monkeypatch.setattr(ac, "_get_provider_chain", lambda: [
        ("openrouter", lambda: (openrouter, "openrouter/model")),
        ("nous", lambda: (nous, "nous-model")),
    ])
    ac._mark_provider_unhealthy("openrouter", model="openrouter/model", reason="rate limit")

    client, model, label = ac._try_payment_fallback(
        "primary", task="compression", reason="provider unavailable",
    )

    assert client is nous
    assert model == "nous-model"
    assert label == "nous"


def test_configured_auxiliary_fallback_can_resolve_a_default_model(monkeypatch):
    """Task fallback entries may omit model and use the provider router's default."""
    client = _client("https://openrouter.ai/api/v1", MagicMock())
    monkeypatch.setattr(ac, "_get_auxiliary_task_config", lambda task: {
        "fallback_chain": [{"provider": "openrouter"}],
    })
    monkeypatch.setattr(ac, "resolve_provider_client", lambda provider, **kwargs: (client, "resolved/default"))

    resolved, model, label = ac._try_configured_fallback_chain(
        "title_generation", "primary", reason="provider unavailable",
    )

    assert resolved is client
    assert model == "resolved/default"
    assert label == "fallback_chain[0](openrouter)"


def test_expired_fallback_route_owner_survives_post_construction_health_check(monkeypatch):
    """The caller that claims an expired recovery probe must not reject its own candidate."""
    base_url = "https://proxy.example/v1"
    client = _client(base_url, MagicMock())
    entry = {"provider": "custom", "model": "model", "base_url": base_url}
    monkeypatch.setattr(ac, "_get_auxiliary_task_config", lambda task: {"fallback_chain": [entry]})
    monkeypatch.setattr(ac, "_resolve_fallback_entry", lambda value: (client, "model"))
    ac._mark_provider_unhealthy("custom", base_url=base_url, model="model", ttl=0, reason="rate limit")

    resolved, model, label = ac._try_configured_fallback_chain(
        "title_generation", "primary", reason="provider unavailable",
    )

    assert resolved is client
    assert model == "model"
    assert label == "fallback_chain[0](custom)"


def test_fallback_label_is_unwrapped_before_recovery_clear():
    """Recovery of a main fallback route must clear the concrete provider record."""
    ac._mark_provider_unhealthy("openrouter", model="model", ttl=0, reason="rate limit")
    client = MagicMock()
    client._hermes_aux_effective_provider = "fallback_providers[0](openrouter)"

    assert ac._effective_provider_for_client(client, "auto") == "openrouter"
    # The expired record gives this caller ownership of the recovery probe.
    assert ac._is_provider_unhealthy("openrouter", model="model") is False
    ac._clear_provider_health("fallback_providers[0](openrouter)", model="model")

    assert not ac._is_provider_unhealthy("openrouter", model="model")


def test_payment_quarantine_is_provider_wide_for_the_main_route(monkeypatch):
    """A provider-wide payment record must skip the main route even when the call has a model."""
    resolve = MagicMock(return_value=(_client("https://openrouter.ai/api/v1", MagicMock()), "model"))
    monkeypatch.setattr(ac, "resolve_provider_client", resolve)
    ac._mark_provider_unhealthy("openrouter", model=None, reason="payment / credit error")

    result = ac._try_main_provider_route("openrouter", "model", "", "", "")

    assert result is None
    resolve.assert_not_called()


def test_exhaustion_warning_uses_the_effective_provider_identity(caplog):
    """The final warning must still fire for an auto route whose client resolved to OpenRouter."""
    caplog.set_level("WARNING", logger="agent.auxiliary_client")
    client = _client("https://openrouter.ai/api/v1", MagicMock())
    client._hermes_aux_effective_provider = "openrouter"
    route = MagicMock(
        client=client,
        resolved_provider="auto",
        base_info="https://openrouter.ai/api/v1",
        final_model="openrouter/model",
        resolved_model="openrouter/model",
        task="compression",
        tag="",
    )

    ac._warn_auxiliary_route_once(route, "provider unavailable", RuntimeError("offline"))

    assert any("all fallbacks exhausted" in record.getMessage() for record in caplog.records)


def test_discovery_checks_route_health_before_constructing_candidate(monkeypatch):
    """An unhealthy Nous route must not run its constructor and leak Nous-only request tags."""
    nous = _client("https://inference-api.nousresearch.com/v1", MagicMock())
    fallback = _client("https://custom.example/v1", MagicMock())
    constructed = []

    def construct_nous():
        constructed.append("nous")
        ac.auxiliary_is_nous = True
        return nous, "nous-model"

    monkeypatch.setattr(ac, "_get_provider_chain", lambda: [
        ("nous", construct_nous),
        ("local/custom", lambda: (fallback, "custom-model")),
    ])
    monkeypatch.setattr(ac, "_is_provider_unhealthy", lambda label, *args, **kwargs: label == "nous")
    ac.auxiliary_is_nous = False

    client, model, label = ac._try_discovery_chain()

    assert client is fallback
    assert model == "custom-model"
    assert label == "local/custom"
    assert constructed == []
    assert ac.auxiliary_is_nous is False


def test_auto_custom_failure_quarantines_concrete_custom_route(monkeypatch):
    """An auto-routed custom endpoint must not be persisted under the abstract ``auto`` label."""
    base_url = "https://openrouter.ai/api/v1"
    client = _client(base_url, MagicMock())
    client._hermes_aux_effective_provider = "custom"
    route = ac._LadderRoute(
        client=client, task="compression", tag="", async_mode=False,
        base_info=base_url, resolved_provider="auto", resolved_model="model",
        resolved_base_url=base_url, resolved_api_key=None, resolved_api_mode=None,
        final_model="model", main_runtime=None, route_info=None, timeout=30.0,
    )
    mark = MagicMock()
    monkeypatch.setattr(ac, "_mark_provider_unhealthy", mark)
    monkeypatch.setattr(ac, "_try_configured_fallback_chain", lambda *args, **kwargs: (None, None, ""))
    monkeypatch.setattr(ac, "_try_main_fallback_chain", lambda *args, **kwargs: (None, None, ""))
    monkeypatch.setattr(ac, "_try_payment_fallback", lambda *args, **kwargs: (None, None, ""))

    list(ac._ladder_provider_fallback(_payment_402("custom"), route))

    mark.assert_called_once()
    assert mark.call_args.args[0] == "local/custom"
    assert mark.call_args.kwargs["base_url"] == base_url


def test_named_custom_route_uses_one_endpoint_scoped_health_identity(tmp_path, monkeypatch):
    """Named custom routes use local/custom consistently through mark, read and clear."""
    base_url = "https://proxy.example/v1"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"providers:\n  foo:\n    base_url: {base_url}\n", encoding="utf-8",
    )
    now = [100.0]
    monkeypatch.setattr(ac.time, "time", lambda: now[0])
    ac._mark_provider_unhealthy("custom:foo", model="model", ttl=60, reason="rate limit")
    identity = ac._auxiliary_route_identity("local/custom", base_url, "model")
    assert ac._read_auxiliary_health(identity) is not None
    assert ac._is_provider_unhealthy("custom:foo", model="model")
    assert ac._is_provider_unhealthy("local/custom", base_url, "model")
    now[0] = 161.0
    assert not ac._is_provider_unhealthy("custom:foo", base_url, "model")
    assert ac._candidate_is_unhealthy("custom:foo", model="model") is False
    ac._clear_provider_health("fallback_chain[0](custom:foo)", model="model")
    assert ac._read_auxiliary_health(identity) is None
    assert not ac._is_provider_unhealthy("local/custom", base_url, "model")


@pytest.mark.parametrize("quarantined_provider", ["custom", "custom:foo"])
def test_named_custom_fallback_health_uses_configured_endpoint(tmp_path, monkeypatch, quarantined_provider):
    """A named fallback without a URL must neither inherit nor evade another endpoint's hold."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  provider: custom\n"
        "  base_url: http://main-custom:8080/v1\n"
        "  model: main-model\n"
        "providers:\n"
        "  foo:\n"
        "    base_url: https://foo.example/v1\n"
        "    api_key: test-key\n"
        "auxiliary:\n"
        "  title_generation:\n"
        "    fallback_chain:\n"
        "      - provider: custom:foo\n"
        "        model: foo-model\n",
        encoding="utf-8",
    )
    held_url = "http://main-custom:8080/v1" if quarantined_provider == "custom" else "https://foo.example/v1"
    ac._mark_provider_unhealthy("custom", base_url=held_url, model="foo-model", reason="rate limit")

    client, model, label = ac._try_configured_fallback_chain(
        "title_generation", "primary", reason="provider unavailable",
    )

    if quarantined_provider == "custom":
        assert client is not None
        assert str(client.base_url).rstrip("/") == "https://foo.example/v1"
        assert (model, label) == ("foo-model", "fallback_chain[0](custom:foo)")
        client.close()
    else:
        assert (client, model, label) == (None, None, "")


def test_model_health_read_keeps_local_provider_hold_when_store_unavailable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("read-only state directory")

    monkeypatch.setattr(ac, "_mark_auxiliary_health", unavailable)
    monkeypatch.setattr(ac, "_read_auxiliary_health", unavailable)
    ac._mark_provider_unhealthy("openrouter", reason="payment error")

    assert ac._is_provider_unhealthy("openrouter", model="foo-model") is True
    assert ac._is_provider_unhealthy("nous", model="foo-model") is False


def _prepared_auto_custom_request():
    base_url = "https://custom.example/v1"
    client = _client(base_url, MagicMock())
    req = ac._PreparedAuxRequest(
        client=client, final_model="model", kwargs={"model": "model"},
        resolved_provider="auto", request_provider="local/custom", resolved_model="model",
        resolved_base_url=base_url, resolved_api_key=None, resolved_api_mode=None,
        effective_timeout=30.0, effective_extra_body={}, base_info=base_url,
    )
    return req, base_url


def test_sync_recovery_clears_concrete_request_provider(monkeypatch):
    """A recovered auto route clears the concrete route record, not the abstract ``auto`` record."""
    req, base_url = _prepared_auto_custom_request()
    clear = MagicMock()
    response = _ok_response("OK")
    first_error = _payment_402("custom")

    def ladder(*args, **kwargs):
        result = yield ac._LadderStep("call", (req.client, req.kwargs))
        return result

    monkeypatch.setattr(ac, "_plan_aux_call", lambda *args, **kwargs: (req, {"max_tokens": None, "main_runtime": None}, {}))
    monkeypatch.setattr(ac, "_relay_sync_completion", MagicMock(side_effect=[first_error, response]))
    monkeypatch.setattr(ac, "_validate_llm_response", lambda value, *args, **kwargs: value)
    monkeypatch.setattr(ac, "_should_retry_same_provider", lambda *args, **kwargs: False)
    monkeypatch.setattr(ac, "_start_recovery_ladder", ladder)
    monkeypatch.setattr(ac, "_clear_provider_health", clear)

    result = ac._call_llm_impl(task="compression", messages=[])

    assert result is response
    clear.assert_called_once_with("local/custom", base_url, "model")


@pytest.mark.asyncio
async def test_async_recovery_clears_concrete_request_provider(monkeypatch):
    """The async recovery driver uses the same concrete route identity as the sync driver."""
    req, base_url = _prepared_auto_custom_request()
    clear = MagicMock()
    response = _ok_response("OK")
    first_error = _payment_402("custom")

    def ladder(*args, **kwargs):
        result = yield ac._LadderStep("call", (req.client, req.kwargs))
        return result

    async_relay = AsyncMock(side_effect=[first_error, response])
    monkeypatch.setattr(ac, "_plan_aux_call", lambda *args, **kwargs: (req, {"max_tokens": None, "main_runtime": None}, {}))
    monkeypatch.setattr(ac, "_relay_async_completion", async_relay)
    monkeypatch.setattr(ac, "_validate_llm_response", lambda value, *args, **kwargs: value)
    monkeypatch.setattr(ac, "_should_retry_same_provider", lambda *args, **kwargs: False)
    monkeypatch.setattr(ac, "_start_recovery_ladder", ladder)
    monkeypatch.setattr(ac, "_clear_provider_health", clear)

    result = await ac._async_call_llm_impl(task="compression", messages=[])

    assert result is response
    clear.assert_called_once_with("local/custom", base_url, "model")
