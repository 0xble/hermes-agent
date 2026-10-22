# Auxiliary Fallback Configuration

Fork patch identity: `auxiliary-fallback-config`

## Contract

The runtime already consumes `auxiliary.<task>.fallback_chain`. Declare an
inert empty-list default for shared side-LLM tasks, maintenance, and title
generation so the config CLI recognizes their valid paths. `auxiliary.review`
is a delegation subagent, not an auxiliary side-LLM consumer.

Absent and empty chains keep existing request behavior. Config check and
migration must not backfill these fields into a sparse current-version file.
Unrelated config saves must preserve model-preset references, including when
all defaults are saved. The shared reference-restoration path removes matched
inert fallback defaults, never an edited nonempty chain.

## Verification And Retirement

Run `tests/hermes_cli/test_auxiliary_fallback_config.py`, the config CLI suites,
`tests/agent/test_auxiliary_client.py`, and the model-preset persistence and
consumer suites. Exercise actual config set/readback without unknown-key
warnings, sparse-config byte preservation, and default-inclusive preset
save/reload with and without preset fallbacks.

Upstream proposal: NousResearch/hermes-agent#122364. Retire when upstream
registers these runtime-read keys with equivalent sparse-config and preset
round-trip behavior. Revert commits carrying this patch identity to roll back.
