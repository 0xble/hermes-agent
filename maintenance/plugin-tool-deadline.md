# Plugin tool deadline

Load when changing plugin tool registration or the executor's per-tool deadline.

Fork patch identity: `plugin-tool-deadline`.

## Required behavior

A plugin may pass `execution_timeout` (seconds) through `PluginContext.register_tool` to declare the bounded budget of a long-running tool. The context rejects booleans, non-numbers, non-finite and non-positive values, so a plugin cannot declare an unbounded tool. The registry stores the value on the tool entry, and the sequential and concurrent executors already extend the generic tool deadline to it through `_extend_tool_timeout`, the same path MCP server timeouts use. A declared budget never shortens the generic deadline. The default is no declaration, so existing plugins keep the generic deadline.

The first consumer is the agents-repository review-candidate plugin, whose reviewer children routinely outlive the generic 420 s sequential deadline. Plugins feature-detect the registration signature to stay loadable on older cores.

## Verification and retirement

Run `scripts/run_tests.sh tests/hermes_cli/test_plugins.py tests/hermes_cli/test_plugins_context.py`. Cover the declared budget reaching the registry entry and `_extend_tool_timeout`, the undeclared default, and rejection of unbounded or malformed values.

Retire this patch when upstream offers an equivalent plugin-declared tool deadline. Rollback restores the generic deadline for plugin tools without a stored-state migration.

Contract tests: `tests/hermes_cli/test_plugins_tool_deadline.py`.
