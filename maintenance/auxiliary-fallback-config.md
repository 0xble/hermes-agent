# Auxiliary Fallback Configuration

Fork patch identity: `auxiliary-fallback-config`

## Contract

The runtime already consumes `auxiliary.<task>.fallback_chain`. Declare an
inert empty-list default for shared side-LLM tasks, maintenance, and title
generation so the config CLI recognizes their valid paths. `auxiliary.review`
is a delegation subagent, not an auxiliary side-LLM consumer.

Absent and empty chains keep existing request behavior. Config check and
migration must not backfill these fields into a sparse current-version file.
Unrelated config saves preserve authored `model_aliases` and bare alias references.
The old preset reference-restoration path and its exclusive tests are retired
with [model presets](model-presets.md). Current consumers use upstream configuration.

## Verification And Retirement

Run `tests/hermes_cli/test_auxiliary_fallback_config.py`, the config CLI suites,
`tests/agent/test_auxiliary_client.py`, and
`tests/hermes_cli/test_model_alias_migration.py`. Exercise actual config
set/readback without unknown-key warnings and sparse-config preservation.

Upstream proposal: NousResearch/hermes-agent#122364. Retire when upstream
registers these runtime-read keys with equivalent sparse-config and authored-alias
round-trip behavior. Revert commits carrying this patch identity to roll back.

Contract tests: `tests/hermes_cli/test_auxiliary_fallback_config.py`, `tests/agent/test_auxiliary_client.py`, `tests/hermes_cli/test_model_alias_migration.py`.
