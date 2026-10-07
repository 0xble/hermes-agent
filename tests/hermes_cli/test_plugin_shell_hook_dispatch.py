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


def _shell_hook_callback(
    *,
    fail_closed: bool,
    matcher: str = "terminal",
    timeout: float = 0.1,
    requires_env=(),
):
    spec = shell_hooks.ShellHookSpec(
        event="pre_tool_call",
        command="python hook.py",
        matcher=matcher,
        timeout=timeout,
        fail_closed=fail_closed,
        requires_env=requires_env,
    )
    return shell_hooks._make_callback(spec)


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestShellHookBackoff:
    def test_fail_open_timeout_backs_off_then_retries(self, monkeypatch):
        import hermes_cli.plugins as plugins_mod
        import hermes_cli.plugins_dispatch as dispatch_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 1.0)
        monkeypatch.setattr(dispatch_mod, "_SHELL_HOOK_WRAPPER_MARGIN_SECS", 0.2, raising=False)
        clock = _FakeClock()
        monkeypatch.setattr(shell_hooks, "_monotonic", clock.monotonic)
        monkeypatch.setattr(shell_hooks, "_SHELL_HOOK_BACKOFF_BASE_SECONDS", 10.0)
        calls = []

        def spawn(spec, stdin_json):
            calls.append(spec.command)
            if len(calls) == 1:
                return _spawn_result(timed_out=True)
            return _spawn_result(returncode=0)

        monkeypatch.setattr(shell_hooks, "_spawn", spawn)
        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=False)]

        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
        assert calls == ["python hook.py"]

        clock.advance(10.1)
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
        assert calls == ["python hook.py", "python hook.py"]

    def test_fail_closed_timeout_is_never_backed_off(self, monkeypatch):
        import hermes_cli.plugins as plugins_mod
        import hermes_cli.plugins_dispatch as dispatch_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 1.0)
        monkeypatch.setattr(dispatch_mod, "_SHELL_HOOK_WRAPPER_MARGIN_SECS", 0.2, raising=False)
        monkeypatch.setattr(shell_hooks, "_spawn", lambda spec, stdin_json: _spawn_result(timed_out=True))
        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=True)]

        first = mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="t1")
        second = mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="t2")
        assert first == second == [{"action": "block", "message": "hook python hook.py failed closed: timed out after 0.1s"}]

    def test_requires_env_skips_before_spawn(self, monkeypatch):
        import hermes_cli.plugins as plugins_mod

        monkeypatch.delenv("HERMES_SHELL_HOOK_REQUIRED", raising=False)
        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 1.0)
        def unexpected_spawn(*_args):
            raise AssertionError("requires_env hook must not spawn")

        monkeypatch.setattr(shell_hooks, "_spawn", unexpected_spawn)
        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [
            _shell_hook_callback(
                fail_closed=False,
                requires_env=("HERMES_SHELL_HOOK_REQUIRED",),
            ),
        ]
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []

    def test_success_resets_exponential_backoff(self, monkeypatch):
        import hermes_cli.plugins as plugins_mod
        import hermes_cli.plugins_dispatch as dispatch_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 1.0)
        monkeypatch.setattr(dispatch_mod, "_SHELL_HOOK_WRAPPER_MARGIN_SECS", 0.2, raising=False)
        clock = _FakeClock()
        monkeypatch.setattr(shell_hooks, "_monotonic", clock.monotonic)
        monkeypatch.setattr(shell_hooks, "_SHELL_HOOK_BACKOFF_BASE_SECONDS", 10.0)
        calls = []

        def spawn(spec, stdin_json):
            calls.append(len(calls) + 1)
            if calls in ([1], [1, 2, 3]):
                return _spawn_result(timed_out=True)
            return _spawn_result(returncode=0)

        monkeypatch.setattr(shell_hooks, "_spawn", spawn)
        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=False)]

        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
        clock.advance(10.1)
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
        clock.advance(10.1)
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
        clock.advance(10.1)
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
        assert calls == [1, 2, 3, 4]

    def test_backoff_doubles_to_cap_and_survives_long_failure_streaks(self, monkeypatch):
        clock = _FakeClock()
        monkeypatch.setattr(shell_hooks, "_monotonic", clock.monotonic)
        spec = shell_hooks.ShellHookSpec(event="pre_llm_call", command="python hook.py")
        windows = []
        for _ in range(1100):  # past 2**1024, where an uncapped float exponent overflows
            shell_hooks._record_shell_hook_failure(spec, "timed out")
            windows.append(round(shell_hooks._shell_hook_backoff[shell_hooks._shell_hook_key(spec)][1] - clock.now, 6))
            clock.advance(windows[-1] + 0.1)
        assert windows[:5] == [60.0, 120.0, 240.0, 480.0, 900.0]
        assert set(windows[4:]) == {shell_hooks._SHELL_HOOK_BACKOFF_MAX_SECONDS}

    def test_backoff_is_scoped_to_matcher_and_home(self, monkeypatch):
        """One failing hook must not silence a sibling with the same command (#353 review)."""
        clock = _FakeClock()
        monkeypatch.setattr(shell_hooks, "_monotonic", clock.monotonic)

        def spec(matcher, home):
            return shell_hooks.ShellHookSpec(
                event="pre_tool_call", command="python hook.py", matcher=matcher, home=home,
            )

        failing = spec("terminal", "/profiles/a")
        shell_hooks._record_shell_hook_failure(failing, "timed out")
        assert shell_hooks.shell_hook_should_skip(spec("terminal", "/profiles/a"))
        assert not shell_hooks.shell_hook_should_skip(spec("web_search", "/profiles/a"))
        assert not shell_hooks.shell_hook_should_skip(spec("terminal", "/profiles/b"))

    def test_spawn_error_backs_off_like_a_timeout(self, monkeypatch):
        import hermes_cli.plugins as plugins_mod

        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 1.0)
        clock = _FakeClock()
        monkeypatch.setattr(shell_hooks, "_monotonic", clock.monotonic)
        calls = []

        def spawn(spec, stdin_json):
            calls.append(1)
            return _spawn_result(error="command not found")

        monkeypatch.setattr(shell_hooks, "_spawn", spawn)
        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [_shell_hook_callback(fail_closed=False)]
        mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={})
        mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={})
        assert calls == [1]

    def test_requires_env_present_runs_hook(self, monkeypatch):
        import hermes_cli.plugins as plugins_mod

        monkeypatch.setenv("HERMES_SHELL_HOOK_REQUIRED", "surface-1")
        monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 1.0)
        calls = []
        monkeypatch.setattr(shell_hooks, "_spawn", lambda spec, stdin_json: calls.append(1) or _spawn_result(returncode=0))
        mgr = PluginManager()
        mgr._hooks["pre_tool_call"] = [
            _shell_hook_callback(fail_closed=False, requires_env=("HERMES_SHELL_HOOK_REQUIRED",)),
        ]
        mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={})
        assert calls == [1]


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
