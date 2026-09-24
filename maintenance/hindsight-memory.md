# Hindsight memory provider

Load this unit when changing `plugins/memory/hindsight/`, the provider's lifecycle
contract with `agent/memory_manager.py` and `agent/agent_init.py`, or the retain
strategy and cron-exclusion behavior.

## Required behavior

- Every attribute `get_tool_schemas()` reads is assigned in `__init__`, not only in
  `initialize()`. `MemoryManager.add_provider()` calls `get_tool_schemas()` BEFORE
  `initialize()` runs, so an attribute that only `initialize()` sets does not exist
  at the first read.
- A provider-construction failure must never be allowed to pass as "memory is off by
  configuration". `agent_init.py` catches any exception from provider setup and sets
  `agent._memory_manager = None`, which disables retain AND recall for every
  subsequent session with no user-visible symptom and a single WARNING in
  `agent.log`. Treat that handler as a silent kill switch, not a safety net.
- Cron sessions skip transcript retention (`_cron_skipped`) and are offered recall
  and reflect but not the retain tool. They must still construct successfully: the
  cron decision belongs in `initialize()`, and the pre-initialize default is the
  non-cron path.
- `retain_strategy` names a bank-defined strategy applied to every stored item, and
  is omitted from the item when unset so the bank keeps deciding. An unknown name is
  silently ignored server-side and the item falls back to unmissioned extraction, so
  a typo degrades quietly rather than failing.
- `retain_context` stays configurable. It carries the attribution boundary telling
  extraction that assistant turns are agent-generated and not the user's decisions.

## Proof surface

- `tests/plugins/memory/test_hindsight_provider.py::TestSchemas::test_get_tool_schemas_before_initialize`
  builds a bare provider and calls `get_tool_schemas()`, which is what
  `add_provider()` does. Every other fixture in that file returns an *initialized*
  provider and therefore cannot catch this class of defect.
- Out-of-tree: `verify-hermes-memory` (hourly, `no-agent`) runs
  `~/.hermes/scripts/hermes-memory-health-gate.py`, which executes `add_provider()`
  against the installed tree and scans `agent.log` for provider-init warnings. It
  exists because the 2026-09-21 outages were both found by hand.

## Provenance and patches

- Fork patch identities: `HERMES-122`, `hindsight-retain-strategy`,
  `hindsight-cron-retention`, `hindsight-bundled-provider`. Local narrow patches on the `v2026.9.14` baseline.
- `HERMES-122` (`6878e95d58`, re-landed `65059fa22d`) defaults `_cron_skipped` in
  `__init__`. Its first landing was reverted hours later by `fc45821e1f`, a backup
  change authored in a worktree created before the fix, whose tree still held the
  pre-fix file and so deleted both the fix and its regression test. Branch freshness
  is the control for that failure mode; the regression test cannot be, because the
  reverting commit removed it in the same diff.
- `hindsight-retain-strategy` (`756c20bd9f`) sets `MemoryItem.strategy` per item.
  Submitted upstream to the Hindsight integration as
  [vectorize-io/hindsight#4570](https://github.com/vectorize-io/hindsight/pull/4570);
  retire the local patch if that lands and is released.
- `hindsight-cron-retention` (`fdb3f2e49d`) withholds the retain tool on cron
  sessions. This is the commit that introduced the `_cron_skipped` read without the
  matching default.

## Retirement condition

Retire `HERMES-122` only if upstream moves the `get_tool_schemas()` call to after
`initialize()`, or stops reading lifecycle state in it. Retire
`hindsight-retain-strategy` when #4570 ships in a released integration version.
Re-run the proof surface against the selected upstream release before retiring
either, without the local implementation present.

## Bundled provider after v2026.9.24

Upstream v2026.9.24 moved Hindsight to an external catalog plugin and removed
`memory.hindsight` from `tools/lazy_deps.LAZY_DEPS`. The fork keeps the bundled
`plugins/memory/hindsight` provider because the catalog version lacks
`retain_strategy: agent-session` and the cron retention exclusion. The provider's
client construction calls `ensure("memory.hindsight")`, so `hindsight-bundled-provider`
keeps that allowlist entry, mirroring the range in its `plugin.yaml`. Without it every
client build raises `FeatureUnavailable` and retain and recall stop.
`tests/plugins/memory/test_memory_lazy_install.py` guards the entry. Drop this patch
only together with the bundled provider.
