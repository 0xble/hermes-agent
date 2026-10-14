# Python plugin hook matchers

- **Patch identity:** `python-plugin-hook-matchers`.
- **Behavior:** `ctx.register_hook(..., tools=...)` accepts exact tool names or a cheap, pure, non-blocking predicate for `pre_tool_call`, `post_tool_call` and `transform_tool_result`; other hooks reject declarations. Sync and async dispatch check applicability before invoking callbacks, and the memory-provider context forwards declarations. A non-matching tool is not delayed or blocked by a hung callback; a matching policy callback still fails closed on timeout, and an undeclared callback retains all-tools behavior.
- **Source surfaces:** `hermes_cli/plugins.py`, `hermes_cli/plugins_dispatch.py`, `hermes_cli/plugins_ledger.py`, `plugins/memory/__init__.py`, `website/docs/developer-guide/plugins/index.md`, `website/docs/user-guide/features/hooks.md`, `tests/hermes_cli/test_plugin_python_hook_dispatch.py`, and `tests/plugins/test_memory_hook_registration.py`.
- **Upstream status:** Upstream-able general dispatcher/API change; no upstream issue or PR was identified for Python hook applicability declarations. Opened as a draft fork PR on `0xble/hermes-agent`.
- **Focused regression:** Run `scripts/run_tests.sh` separately for `tests/hermes_cli/test_plugin_python_hook_dispatch.py`, `tests/hermes_cli/test_plugin_shell_hook_dispatch.py`, `tests/hermes_cli/test_plugins.py`, and `tests/plugins/test_memory_hook_registration.py`.
- **Retirement:** Remove when an upstream release provides the same registration API, pre-wrapper applicability check, and regressions.
- **Rollback:** Revert the commit carrying `Fork-Patch: python-plugin-hook-matchers`.
