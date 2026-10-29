# Eager plugin tools

Load when changing plugin tool registration or tool-search deferral classification.

Fork patch identity: `eager-plugin-tools`.

## Required behavior

Plugin owners may pass `eager=True` through `PluginContext.register_tool` to keep a tool's full schema on the direct model-visible surface. The registry stores the flag. The default is false, so existing plugins keep their deferred behavior. Explicit user `tools.tool_search.defer` entries take precedence over eager registration; bridge tools remain non-deferrable.

Classification, scoped deferrable names, schema description, and bridge invocation use the same predicate. An eager tool is not cataloged, and `tool_call` rejects it with the existing direct-invocation error.

The first consumers are the agents-repository goal and loop lifecycle plugins. Those plugins feature-detect the registration signature to stay loadable on older cores. No tool names are special-cased in Hermes core.

## Verification and retirement

Run `scripts/run_tests.sh tests/tools/test_tool_search.py tests/tools/test_registry.py tests/hermes_cli/test_plugins.py`. Cover eager visibility, ordinary plugin deferral, explicit user override, and describe/call rejection. Run the agents lifecycle plugin registration tests and fresh-session automation routing Cases with normal tool search.

Retire this patch when upstream offers an equivalent plugin-owned direct-tool registration contract. Rollback restores default plugin deferral without a stored-state migration.
