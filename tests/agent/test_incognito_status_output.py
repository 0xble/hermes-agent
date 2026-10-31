"""A private agent's status reaches the user through status_callback, never raw stdout.

A gateway redirects stdout to gateway.log, which the privacy log filter never sees.
"""
import secrets

import pytest

from agent.incognito import incognito_scope


def build_agent(private, statuses, *, quiet=True):
    from run_agent import AIAgent
    agent = AIAgent(model="test-model", api_key="test-key", base_url="http://127.0.0.1:1/v1", platform="telegram",
                    session_id="private-status" if private else "public-status", enabled_toolsets=[],
                    quiet_mode=quiet, skip_memory=True, skip_context_files=True, incognito=private)
    agent.status_callback = lambda kind, message: statuses.append((kind, str(message)))
    return agent


@pytest.mark.parametrize("private", [True, False])
def test_warning_reaches_status_callback_and_only_public_stdout(private, capsys):
    statuses = []
    agent = build_agent(private, statuses)
    canary = "canary-" + secrets.token_hex(12)
    with incognito_scope(private, agent.session_id):
        agent._emit_warning(f"Provider said: {canary}")
    assert ("warn", f"Provider said: {canary}") in statuses
    assert (canary in capsys.readouterr().out) is not private


@pytest.mark.parametrize("private", [True, False])
def test_provider_401_body_stays_off_private_stdout(private, capsys, monkeypatch):
    import agent.conversation_loop as loop
    from agent.turn_recovery import _print_nous_401_diagnostics
    monkeypatch.setattr(loop, "_print_nous_entitlement_guidance", lambda *a: False)
    agent = build_agent(private, [])
    canary = "canary-" + secrets.token_hex(12)
    error = Exception("unauthorized")
    error.body = {"error": f"request echoed {canary}"}
    with incognito_scope(private, agent.session_id):
        _print_nous_401_diagnostics(agent, error)
    output = capsys.readouterr().out
    assert (canary in output) is not private
    assert ("Nous 401" in output) is not private


@pytest.mark.parametrize("private", [True, False])
def test_verbose_tool_progress_stays_off_private_stdout(private, capsys):
    """A non-quiet gateway session (a verbose session policy) prints tool arguments and results."""
    from agent import tool_executor
    agent = build_agent(private, [], quiet=False)
    capsys.readouterr()
    canary = "canary-" + secrets.token_hex(12)
    with incognito_scope(private, agent.session_id):
        tool_executor._begin_tool_execution(agent, tool_executor._ToolCallRef(
            name="web_search", args={"query": canary}, task_id="t", call_id="c", trace=[]), 1)
        tool_executor._print_tool_completed(agent, 1, 0.1, f"result {canary}")
    assert (canary in capsys.readouterr().out) is not private


@pytest.mark.parametrize("private", [True, False])
def test_ephemeral_prompt_preview_stays_off_private_stdout(private, capsys):
    """A non-quiet agent previews its ephemeral prompt at init, and a gateway's can carry context."""
    from run_agent import AIAgent
    canary = "canary-" + secrets.token_hex(8)
    AIAgent(model="test-model", api_key="test-key", base_url="http://127.0.0.1:1/v1", platform="telegram",
            session_id="private-init" if private else "public-init", enabled_toolsets=[], quiet_mode=False,
            skip_memory=True, skip_context_files=True, incognito=private,
            ephemeral_system_prompt=f"Context: {canary}")
    assert (canary in capsys.readouterr().out) is not private
