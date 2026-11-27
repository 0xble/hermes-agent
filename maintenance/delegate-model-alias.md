# Explicit delegation model alias

Fork patch identity: `delegate-model-alias`.

Deliberate fork-only patch, chosen by Brian on 2026-11-20. Upstream Hermes removed the
per-call `delegate_task(model=...)`, and the fork later retired `model_presets`, `@name`,
`delegation.by_parent` and named subagents in favor of Advanced Delegation roles. This patch
does not bring those back. Roles (the model-map `option`) stay the default way to choose a
child's model. The alias field exists only for the case where the user explicitly names a model.

## Required behavior

- Each `delegate_task` task accepts an optional `model`. Its enum is the alias set `/model`
  resolves (`hermes_cli.model_switch._load_direct_aliases`: `model_aliases` plus `model.aliases`),
  read whenever the schema is built, so a new alias reaches chat and delegation with no second
  list. The field is omitted when no aliases are configured. An unknown name refuses that task
  alone with an error listing the configured aliases.
- The `model` description says to set it only when the user explicitly asks for a specific
  model and otherwise to choose a role. Keep it short.
- When both `model` and `option` are given, `model` wins and the backend is not consulted. The
  child is model-pinned, and only the alias's own `fallback_providers` back it, filtered to the
  same model by the explicit-model-fallback rule. Without one it has no fallback chain, and it
  never borrows `delegation.fallback_providers` or a role's chain. The result's
  `route` reports `backend: model_alias` with the alias as `label`.
- Each task also accepts an optional `reasoning_effort` (`none`, `low`, `medium`, `high`,
  `xhigh`). When set, it is the child's final level, ahead of the alias's or role's default, the
  `delegation` section and the parent's level. When unset, existing precedence is unchanged.
  Other values refuse that task alone.
- `model` and `reasoning_effort` are core-owned task parameters (`RESERVED_TASK_PARAMETERS`).
  A delegation backend cannot redefine them.

## Source surfaces

`tools/delegate_tool_routes.py` (`core_task_parameters`, `_alias_route`, `_task_effort`,
`resolve_task_routes`), `tools/delegate_tool.py` (`_build_dynamic_schema_overrides`),
`agent/delegation_provider.py` (`RESERVED_TASK_PARAMETERS`), and
`agent/turn_failure_routes.py` (fallback-path copy for alias children).

## Verification

Run `scripts/run_tests.sh tests/tools/test_delegate_task_model_alias.py`. The live canary is a
real `delegate_task(model=<alias>, reasoning_effort=<level>)` spawn whose result `route` shows the
alias model and level, next to a spawn without `model` that still reports its role.

## Retirement and rollback

Retire only if upstream restores an explicit per-task model choice tied to `model_aliases`. To
roll back, revert the commits carrying `Fork-Patch: delegate-model-alias`. No state or config
migration is involved. Tasks then lose the two fields, and roles keep working.

Contract tests: `tests/tools/test_delegate_task_model_alias.py`.
