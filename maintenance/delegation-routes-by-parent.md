# Delegation Routes By Parent

Load this unit when changing `delegation.by_parent`, model preset expansion,
child credentials or reasoning, or plugin lifecycle launches.

## Required Behavior

- `delegation.by_parent` is a mapping from an exact live parent model ID or a
  bare `@preset` reference to `inherit` or a bare `@preset` reference.
- Preset keys match the preset's resolved model, not its provider. Duplicate
  resolved models are errors, including a literal and a preset that alias the
  same model. Unknown presets, malformed keys, and unsupported values raise
  `ModelPresetError` during config loading, never silently fall back.
- Selection uses `parent_agent.model` at spawn time. A model switch or fallback
  changes the next selection. An unmatched parent keeps the existing pinned
  delegation route and identical child runtime arguments.
- `inherit` removes all route settings from the delegation block: model,
  provider, endpoint, key, API mode, request overrides, fallback policy,
  reasoning effort, command, and args. The existing pure-inherit credential
  branch supplies the parent's current model, provider, endpoint, credentials,
  fallback chain, and reasoning. Global operational settings remain global.
- A target preset replaces those same settings with its provider, model,
  optional reasoning effort, and fallbacks as `fallback_providers`. Missing
  fallbacks on a pinned target do not borrow the parent's chain. An explicit
  parent session reasoning pick retains its existing precedence.
- Internal `credentials_cfg` owners such as `/review` bypass `by_parent`
  entirely, including an empty credential mapping. Their reasoning continues
  to use the global delegation setting, not the internal credential owner.
- Preset expansion does not mutate authored config. Unrelated saves preserve
  the authored parent keys and target references. Each new spawn reads the
  shared signature-keyed config loader, so config edits need no restart.

## Entry Points

`hermes_cli/model_presets.py` validates the map and expands its parent keys at
the existing configuration boundary. Target references remain strings so general
config expansion stays idempotent and serializable. Its `delegation_runtime_config`
resolves target routes at the delegation loader boundary. `tools/delegate_tool_config.py::_select_routing_cfg`
is the shared runtime selector.

`tools/delegate_tool.py::delegate_task` selects before building children.
Synchronous, background, and batch execution use those already-built children.
`agent/subagent_lifecycle.py::SubagentLifecycleService.launch` uses the same
selector unless a launch explicitly supplies a provider. Explicit launch
models still win. Selected ordinary routes carry their reasoning settings to
child construction, while compression caps and other operational knobs still
read the global delegation section.

`tools/delegation_resume.py` returns recovery instructions, not a new child.
The parent's subsequent normal spawn selects from its then-current model and
config. `tools/delegate_tool_dispatch.py` dispatches already-built children,
so it must not re-select a route after admission or while queued. Nested
children select against their own live parent model in exactly the same way.

The gateway's `_CACHE_BUSTING_CONFIG_KEYS` contains no delegation settings.
Do not add one just for this feature: the per-spawn loader, not parent-agent
cache invalidation, owns live delegation configuration.

## Provenance

Fork patch identity: `delegation-routes-by-parent`.

Fork-only route policy for keeping selected parent models on their own routes
while ordinary parents retain the pinned worker model. Upstream delegation
construction, fallback normalization, and reasoning precedence remain owners
of their respective behavior.

## Verification

```bash
scripts/run_tests.sh tests/tools/test_delegate_by_parent.py \
  tests/hermes_cli/test_delegation_by_parent_presets.py \
  tests/agent/test_subagent_by_parent.py tests/agent/test_subagent_lifecycle.py
```

These tests exercise real config loading and child construction, replacing only
inference execution. They cover inheritance, unmatched sol/opus and fallback
parents, preset targets, live edits and switches, nested children, complete
internal-review runtime parity, lifecycle launch precedence, strict validation,
and authored-config round trips. Run the existing delegate and model preset
suites as regression coverage too.

## Retirement And Rollback

Retire this patch if upstream supplies equivalent live-parent route selection
at both delegation entry points. To roll back, first remove `by_parent` from
consumer configuration, then revert this patch. Existing inline or preset
pinned delegation routes remain valid without it.
