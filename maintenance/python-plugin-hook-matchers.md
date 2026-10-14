# Python plugin hook matchers

- **Patch identity:** `python-plugin-hook-matchers`.
- **Behavior:** `ctx.register_hook("pre_tool_call", callback, tools=...)` accepts exact tool names or a predicate. The dispatcher checks the declaration before the bounded callback worker and timeout-suppression path. A non-matching tool is not delayed or blocked by a hung callback; a matching callback still fails closed on timeout, and an undeclared callback retains all-tools behavior.
- **Source surfaces:** `hermes_cli/plugins.py`, `hermes_cli/plugins_dispatch.py`, `website/docs/developer-guide/plugins/index.md`, `website/docs/user-guide/features/hooks.md`, and `tests/hermes_cli/test_plugin_python_hook_dispatch.py`.
- **Upstream status:** Upstream-able general dispatcher/API change; no upstream issue or PR was identified for Python hook applicability declarations. Opened as a draft fork PR on `0xble/hermes-agent`.
- **Focused regression:** `scripts/run_tests.sh tests/hermes_cli/test_plugin_python_hook_dispatch.py` and `scripts/run_tests.sh tests/hermes_cli/test_plugin_shell_hook_dispatch.py`.
- **Retirement:** Remove when an upstream release provides the same registration API, pre-wrapper applicability check, and regressions.
- **Rollback:** Revert the commit carrying `Fork-Patch: python-plugin-hook-matchers`.
