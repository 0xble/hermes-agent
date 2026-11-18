# Retired model presets

Fork patch identity: `model-presets`.

Retired during the v0.23.1 integration. Upstream `model_aliases` is the sole
model-label registry. The fork preset expander, `model_preset`, `@name`
syntax, concrete-expansion persistence and named custom-subagent expansion
(`delegation.subagents`, task `subagent_type`) are removed with their exclusive
tests. The live configuration used no named custom subagents.

Upstream model switching and Advanced Delegation's `model_map` and
`profile_routes` replace selection and named roles. Request-time alias names
remain supported at persisted boundaries by [dynamic aliases](dynamic-model-aliases.md).
The automatic v54 migration and its dry-run command carry old configuration
and JSON cron/topic references to those owners. This historical identity stays
here so published Fork-Patch trailers retain an accountable retirement record.

The historical named Custom Subagents implementation from commit
`2a56de65fd8d2ee561eaae10663babaaa302486c` is also retired. Its named-role
selection is replaced by upstream delegation backends and Advanced Delegation
`model_map` options or `profile_routes`. No configured named custom roles need
translation. Parent-only durable knowledge writes and child lifecycle ownership
remain with upstream's core lifecycle rather than custom route definitions.
