"""The dispatcher wrapper must not preempt a shell hook's own gate (#132096)."""

from __future__ import annotations

import time

from agent import shell_hooks
from hermes_cli.plugins import PluginManager, _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE


def _spawn_result(**overrides):
    result = {
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "elapsed_seconds": 0.0,
        "error": None,
    }
    result.update(overrides)
    return result


def _shell_hook_callback(*, fail_closed: bool, matcher: str = "terminal", timeout: float = 0.1):
    spec = shell_hooks.ShellHookSpec(
        event="pre_tool_call",
        command="python hook.py",
        matcher=matcher,
        timeout=timeout,
        fail_closed=fail_closed,
    )
    return shell_hooks._make_callback(spec)


class TestShellHookWrapperTimeout:
    def test_wrapper_timeout_fails_open_for_fail_open_hook(self, monkeypatch):
        """Dispatcher wrapper expiry with fail_closed=false must not block (#132096)."""
        import hermes_cli.plugins as plugins_mod
        import hermes_cli.plugins_dispatch as dispatch_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0.15)
        monkeypatch.setattr(dispatch_mod, "_SHELL_HOOK_WRAPPER_MARGIN_SECS", 0.2, raising=False)
        monkeypatch.setattr(
            shell_hooks,
            "_spawn",
            lambda spec, stdin_json: (time.sleep(5), _spawn_result())[1],
        )

        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=False)]
        t0 = time.monotonic()
        results = mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="t1")
        assert time.monotonic() - t0 < 3.0
        assert results == []

    def test_wrapper_timeout_fails_closed_when_hook_declares_it(self, monkeypatch):
        """A shell hook that declares fail_closed=true keeps the block."""
        import hermes_cli.plugins as plugins_mod
        import hermes_cli.plugins_dispatch as dispatch_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0.15)
        monkeypatch.setattr(dispatch_mod, "_SHELL_HOOK_WRAPPER_MARGIN_SECS", 0.2, raising=False)
        monkeypatch.setattr(
            shell_hooks,
            "_spawn",
            lambda spec, stdin_json: (time.sleep(5), _spawn_result())[1],
        )

        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=True)]
        results = mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="t1")
        assert results == [{"action": "block", "message": _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE}]

    def test_suppression_window_never_blocks_unmatched_or_fail_open_tools(self, monkeypatch):
        """A timeout must not gate unmatched tools or fail-open matching tools."""
        import hermes_cli.plugins as plugins_mod
        import hermes_cli.plugins_dispatch as dispatch_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0.15)
        monkeypatch.setattr(dispatch_mod, "_SHELL_HOOK_WRAPPER_MARGIN_SECS", 0.2, raising=False)
        monkeypatch.setattr(
            shell_hooks,
            "_spawn",
            lambda spec, stdin_json: (time.sleep(5), _spawn_result())[1],
        )

        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=False)]
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="t1") == []
        assert mgr.invoke_hook("pre_tool_call", tool_name="read_file", args={}, tool_call_id="t2") == []
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="t3") == []


class TestShellLayerDecisionWins:
    def test_shell_timeout_fails_open_before_wrapper_expires(self, monkeypatch):
        """The shell layer's own fail-open timeout decision is final."""
        import hermes_cli.plugins as plugins_mod
        import hermes_cli.plugins_dispatch as dispatch_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 30.0)
        monkeypatch.setattr(dispatch_mod, "_SHELL_HOOK_WRAPPER_MARGIN_SECS", 10.0, raising=False)
        monkeypatch.setattr(shell_hooks, "_spawn", lambda spec, stdin_json: _spawn_result(timed_out=True))

        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=False)]
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="t1") == []
        assert mgr._hook_timeout_suppressed_until == {}

    def test_shell_timeout_block_carries_the_hook_message(self, monkeypatch):
        """A fail-closed shell timeout names the shell hook's decision."""
        import hermes_cli.plugins as plugins_mod
        import hermes_cli.plugins_dispatch as dispatch_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 30.0)
        monkeypatch.setattr(dispatch_mod, "_SHELL_HOOK_WRAPPER_MARGIN_SECS", 10.0, raising=False)
        monkeypatch.setattr(shell_hooks, "_spawn", lambda spec, stdin_json: _spawn_result(timed_out=True))

        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=True)]
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="t1") == [
            {"action": "block", "message": "hook python hook.py failed closed: timed out after 0.1s"},
        ]
