"""Sync and async auxiliary calls obey the same configured attempt budget."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import hermes_yaml as yaml

from agent import auxiliary_client as aux
from hermes_constants import get_hermes_home


@pytest.fixture
def transport():
    """Keep real planning and config reads, replace the provider boundary."""
    client = MagicMock()
    client.base_url = "https://aux.example/v1"
    with ExitStack() as stack:
        stack.enter_context(patch.object(aux, "_resolve_task_provider_model", return_value=(
            "custom", "test-model", client.base_url, "test-key", "chat_completions")))
        stack.enter_context(patch.object(aux, "_get_cached_client", return_value=(client, "test-model")))
        stack.enter_context(patch.object(aux, "_validate_llm_response", side_effect=lambda r, *a, **k: r))
        stack.enter_context(patch.object(aux, "_try_configured_fallback_chain", return_value=(None, None, "")))
        stack.enter_context(patch.object(aux, "_try_main_agent_model_fallback", return_value=(None, None, "")))
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("configured,retries", [(0, 0), (2, 2), (9, 6), (-1, 0), ("invalid", 2), (None, 2)])
async def test_transient_retry_budget_is_configured_and_bounded(transport, async_mode, configured, retries):
    config = {"auxiliary": {}}
    if configured is not None:
        config["auxiliary"]["transient_retries"] = configured
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    error = ConnectionError("Connection reset by peer")
    create = AsyncMock(side_effect=error) if async_mode else MagicMock(side_effect=error)
    transport.chat.completions.create = create
    with patch.object(aux.time, "sleep") as sync_sleep, patch("asyncio.sleep", new_callable=AsyncMock) as async_sleep:
        with pytest.raises(ConnectionError):
            if async_mode:
                await aux.async_call_llm(task="session_search", messages=[{"role": "user", "content": "find"}])
            else:
                aux.call_llm(task="session_search", messages=[{"role": "user", "content": "find"}])
    assert create.call_count == 1 + retries
    sleeps = async_sleep if async_mode else sync_sleep
    assert [c.args[0] for c in sleeps.call_args_list] == [min(2.0 ** n, 8.0) for n in range(retries)]
    assert (sync_sleep if async_mode else async_sleep).call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
async def test_goal_judge_full_timeout_does_not_retry(transport, async_mode):
    from openai import APITimeoutError
    import httpx
    from hermes_cli.goals import DEFAULT_JUDGE_TIMEOUT, _goal_judge_timeout

    (get_hermes_home() / "config.yaml").write_text("{}\n", encoding="utf-8")
    error = APITimeoutError(request=httpx.Request("POST", transport.base_url))
    create = AsyncMock(side_effect=error) if async_mode else MagicMock(side_effect=error)
    transport.chat.completions.create = create
    with patch.object(aux.time, "sleep"), patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(APITimeoutError):
            if async_mode:
                await aux.async_call_llm(task="goal_judge", messages=[{"role": "user", "content": "judge"}])
            else:
                aux.call_llm(task="goal_judge", messages=[{"role": "user", "content": "judge"}])
    assert create.call_count == 1
    assert create.call_args.kwargs["timeout"] == DEFAULT_JUDGE_TIMEOUT
    assert _goal_judge_timeout() == DEFAULT_JUDGE_TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("configured,budget", [(None, 300.0), (30, 30.0), ("80", 80.0), (120, 120.0), (900, 900.0)])
async def test_compression_explicit_timeout_reaches_transport_and_host(transport, async_mode, configured, budget):
    from agent.conversation_compression_deadline import resolve_context_compression_timeouts

    config = {"auxiliary": {"compression": {}}, "compression": {
        "context_timeout_seconds": 40, "context_total_ceiling_seconds": 150}}
    if configured is not None:
        config["auxiliary"]["compression"]["timeout"] = configured
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    create = AsyncMock(return_value={"summary": "ok"}) if async_mode else MagicMock(return_value={"summary": "ok"})
    transport.chat.completions.create = create
    if async_mode:
        await aux.async_call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])
    else:
        aux.call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])
    assert create.call_args.kwargs["timeout"] == budget
    assert resolve_context_compression_timeouts() == (max(40, budget), max(150, budget))


def test_compression_timeout_is_profile_local_and_frozen_policy_wins(transport, tmp_path, monkeypatch):
    from agent.auxiliary_task_config import session_auxiliary_scope

    a, b = tmp_path / "a", tmp_path / "b"
    for home, timeout in ((a, 35), (b, 85)):
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({"auxiliary": {
            "compression": {"timeout": timeout}}}), encoding="utf-8")
    transport.chat.completions.create.return_value = {"summary": "ok"}
    for home, expected in ((a, 35), (b, 85), (a, 35)):
        monkeypatch.setenv("HERMES_HOME", str(home))
        aux.call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])
        assert transport.chat.completions.create.call_args.kwargs["timeout"] == expected
        with session_auxiliary_scope({"compression": {"timeout": 25}}):
            aux.call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])
            assert transport.chat.completions.create.call_args.kwargs["timeout"] == 25


def _call_compression(transport):
    transport.chat.completions.create.return_value = {"summary": "ok"}
    aux.call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])
    return transport.chat.completions.create.call_args.kwargs["timeout"]


def test_frozen_worker_snapshot_timeout_beats_live_file(transport, monkeypatch):
    import agent.safe_worker_policy as policy

    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(
        {"auxiliary": {"compression": {"timeout": 45}}}), encoding="utf-8")
    monkeypatch.setattr(policy, "worker_config_snapshot", lambda: {
        "auxiliary": {"compression": {"timeout": 200}}})
    assert _call_compression(transport) == 200


def test_default_config_snapshot_does_not_pin_the_compression_floor(transport, monkeypatch):
    import json
    import agent.safe_worker_policy as policy
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(
        {"auxiliary": {"compression": {"timeout": 45}}}), encoding="utf-8")
    # The worker bind json-round-trips the frozen copy of DEFAULT_CONFIG.
    snapshot = json.loads(json.dumps(DEFAULT_CONFIG))
    monkeypatch.setattr(policy, "worker_config_snapshot", lambda: snapshot)
    assert _call_compression(transport) == 300.0


def test_bypass_worker_has_no_authored_compression_timeout(transport, monkeypatch):
    import agent.safe_worker_policy as policy

    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(
        {"auxiliary": {"compression": {"timeout": 45}}}), encoding="utf-8")
    monkeypatch.setattr(policy, "worker_ignores_user_config", lambda: True)
    monkeypatch.setattr(policy, "worker_config_snapshot", lambda: {
        "auxiliary": {"compression": {"timeout": 200}}})
    assert _call_compression(transport) == 300.0


def test_env_expanded_timeout_is_explicit(transport, monkeypatch):
    monkeypatch.setenv("AUX_COMPRESSION_TIMEOUT", "45")
    (get_hermes_home() / "config.yaml").write_text(
        "auxiliary:\n  compression:\n    timeout: ${AUX_COMPRESSION_TIMEOUT}\n", encoding="utf-8")
    assert _call_compression(transport) == 45.0


def test_managed_overlay_timeout_is_explicit(transport, monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(yaml.safe_dump(
        {"auxiliary": {"compression": {"timeout": 77}}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (get_hermes_home() / "config.yaml").write_text("{}\n", encoding="utf-8")
    assert _call_compression(transport) == 77.0
