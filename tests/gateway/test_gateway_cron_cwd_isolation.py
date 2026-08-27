"""Regression coverage for cron-to-gateway cwd contamination (#81451)."""

from pathlib import Path

import pytest


@pytest.fixture
def cwd_pair(tmp_path):
    gateway_cwd = tmp_path / "gateway"
    cron_cwd = tmp_path / "cron"
    for path in (gateway_cwd, cron_cwd):
        path.mkdir()
    (gateway_cwd / "AGENTS.md").write_text(
        "GATEWAY_CONTEXT_MARKER", encoding="utf-8"
    )
    (cron_cwd / "AGENTS.md").write_text("CRON_CONTEXT_MARKER", encoding="utf-8")
    return gateway_cwd, cron_cwd


def test_cron_first_cwd_mutation_cannot_redirect_new_gateway_session(
    tmp_path, monkeypatch, cwd_pair
):
    """A gateway turn created after a cron override still uses its startup cwd."""
    from agent.prompt_builder import build_context_files_prompt
    from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
    from gateway import run as gateway_run
    from gateway.config import Platform
    from gateway.platforms.base import _default_docker_workspace_host_roots
    from gateway.runtime_footer import format_runtime_footer
    from gateway.session import SessionContext, SessionSource
    from gateway.session_context import reset_session_vars
    from tools.code_execution_tool import _resolve_child_cwd
    from tools.delegate_tool import _resolve_workspace_hint
    from tools.file_tools import _resolve_base_dir
    from tools.terminal_tool import _get_env_config, clear_session_cwd

    gateway_cwd, cron_cwd = cwd_pair
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    # Gateway startup resolves and freezes A before the scheduler can run.
    monkeypatch.setattr(gateway_run, "_GATEWAY_TERMINAL_CWD", str(gateway_cwd))

    # Reproduce the observed ordering: the cron mutates process-global state
    # first, and only then does an unrelated interactive message bind a session.
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(cron_cwd))

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._gateway_terminal_cwd = str(gateway_cwd)
    context = SessionContext(
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="cwd-isolation-chat",
            chat_type="dm",
        ),
        connected_platforms=[Platform.TELEGRAM],
        home_channels={},
        session_key="cwd-isolation-session",
    )

    tokens = runner._set_session_env(context)
    try:
        assert resolve_agent_cwd() == gateway_cwd
        assert resolve_context_cwd() == gateway_cwd

        prompt = build_context_files_prompt(cwd=str(resolve_context_cwd()))
        assert "GATEWAY_CONTEXT_MARKER" in prompt
        assert "CRON_CONTEXT_MARKER" not in prompt

        # Prompt and every relative-path tool surface must agree on A.
        assert Path(_get_env_config()["cwd"]) == gateway_cwd
        assert _resolve_base_dir(context.session_key) == gateway_cwd
        assert _resolve_workspace_hint(object()) == str(gateway_cwd)
        assert runner._gateway_command_cwd() == str(gateway_cwd)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
        monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
        assert _default_docker_workspace_host_roots() == [gateway_cwd.resolve()]
        assert (
            format_runtime_footer(
                model=None,
                context_tokens=0,
                context_length=None,
                cwd="",
                fields=("cwd",),
            )
            == ""
        )
        assert (
            Path(
                _resolve_child_cwd(
                    "project", str(staging_dir), context.session_key
                )
            )
            == gateway_cwd
        )
    finally:
        runner._clear_session_env(tokens)
        clear_session_cwd(context.session_key)
        reset_session_vars()


def test_cron_first_cwd_mutation_cannot_redirect_api_request(
    monkeypatch, cwd_pair
):
    """The OpenAI-compatible API surface shares the gateway process and cron."""
    from agent.runtime_cwd import resolve_tool_cwd
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.session_context import clear_session_vars, reset_session_vars

    gateway_cwd, cron_cwd = cwd_pair
    adapter = object.__new__(APIServerAdapter)
    adapter._gateway_cwd = str(gateway_cwd)
    monkeypatch.setenv("TERMINAL_CWD", str(cron_cwd))

    tokens = adapter._bind_api_server_session(
        chat_id="api-chat",
        session_key="api-session",
        session_id="api-session",
    )
    try:
        assert resolve_tool_cwd() == str(gateway_cwd)
    finally:
        clear_session_vars(tokens)
        reset_session_vars()
