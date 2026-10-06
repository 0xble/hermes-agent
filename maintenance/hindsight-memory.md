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
- A queued append-mode retain is the only copy of its turns (`sync_turn` drops them from
  `_session_turns` once queued), so the writer keeps a failed job in an ordered, bounded
  backlog and retries it instead of discarding it (`TestRetainRetry`). Tag settings
  (`retain_tags`, `recall_tags`) accept comma-separated strings and reach the SDK as lists.
- Hermes-generated user turns (notices, goal continuations, heartbeat and `/loop` wakeups, every
  turn of a cron run, recovery notes) neither key automatic recall nor enter retained transcripts. Both
  gates call `agent.synthetic_prompt.human_prompt_text`, which reads the turn's runtime-owned
  `display_kind` and platform and each producer's own formatter boundary, keeping any human text
  merged after that boundary. Add a new generated prompt there, beside its producer constant,
  rather than in a provider. `memory.recall_synthetic_turns` (default off) restores recall on
  generated turns. Proof: `tests/agent/test_synthetic_prompt.py`.
- Template boundary rule: when a template's closing paragraph appears more than once, the LAST
  copy is the generated boundary. Generated text is never classified as human, which is #320's
  invariant. A revised goal continuation (with its "This goal has been revised" block) has no
  provable end and is generated in full. Accepted limitation: a person's message that the gateway
  text-merged into a pending goal, kanban, heartbeat or `/loop` prompt and that quotes that
  prompt's exact closing paragraph keeps only the text after the quote for recall and retention,
  and a message merged into a revised continuation keeps none. The message itself is still
  delivered and answered. Text cannot tell such a quote from a payload copy, and carrying the human
  part as structured metadata through every merge path was judged too wide for this patch.
- Buffered recall across generated turns. Default (async) Hindsight injects the result the
  post-turn `queue_prefetch` computed for the PREVIOUS turn. Generated turns neither consume nor
  queue, so in human A -> generated S -> human B, B injects the recall keyed on A, the latest human
  intent (before this gate it was keyed on S's generated text). Review raised that A's result can
  be stale. Discarding it on every suppressed turn was rejected: B would get no automatic recall,
  and goal-heavy sessions put many generated turns between human ones. Measured on 30 days of
  state.db top-level sessions, 72% of human -> generated -> human gaps are within 30 minutes
  (median 11 min), against 90% of direct human -> human gaps. The defect is unbounded age, so
  `MemoryManager` records when it last queued (`queue_prefetch_all`) and, at the next
  `prefetch_all`, calls every provider's `discard_prefetch()` once that is older than
  `memory.prefetch_max_age_seconds` (default 1800, `0` = no limit). The clock is wall time because
  macOS's monotonic clock stops during sleep. It is manager-level so every buffering provider is
  covered: Hindsight bumps its generation (also drops an in-flight worker), RetainDB clears its
  caches, Honcho drops its pending dialectic. Mem0 keys its buffer on the query and OpenViking,
  ByteRover, Holographic and Supermemory recall live, so they keep the no-op default. The buffer
  lives on the provider instance owned by one agent's manager (each `load_memory_provider` call
  builds a new instance), so it does not cross sessions, and Hindsight's `on_session_switch`
  already drops it on /new, /resume, /branch and compression. In a shared chat it can carry one
  participant's recall to another's next turn, which every async turn already did before this gate. Proof:
  `tests/agent/test_synthetic_prompt.py` (`test_buffered_recall_*`).

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
  `hindsight-cron-retention`, `hindsight-bundled-provider`,
  `memory-note-not-authoritative`, `synthetic-prompt-memory-gate`. Local narrow patches
  on the `v2026.9.14` baseline.
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
- `synthetic-prompt-memory-gate` moves the generated-prompt inventory to
  `agent/synthetic_prompt.py` and gates the core turn-start and post-turn recall paths on
  it, plus retention through `sync_all` provenance. It is core because the prefetch call
  sites and turn provenance live in `agent/turn_context.py` and `run_agent.py`. No
  upstream issue or PR covered synthetic-turn recall as of 2026-10-05. Retire it when
  upstream skips provider prefetch for runtime-generated turns. Roll back by reverting
  its commit, which restores the `hindsight-session-lifecycle` retention-only filter in
  `plugins/memory/hindsight/retention.py`.

- `memory-note-not-authoritative` changes the note `build_memory_context_block()` puts
  before every provider recall. The upstream note called recalled memory
  "authoritative reference data" that "should inform all responses". That tells the
  model to trust stale or derived memories and skip an explicit recall, which
  contradicts the deployed evidence policy. The fork uses the wording of upstream
  [NousResearch/hermes-agent#89283](https://github.com/NousResearch/hermes-agent/pull/89283)
  (open), which also addresses #31584 and #66888. `_INTERNAL_NOTE_RE` still strips the
  older wordings from provider output. Proof: `TestMemoryContextFencing` in
  `tests/agent/test_memory_provider.py`. Retire when #89283 or an equivalent
  non-authoritative note ships in a selected upstream release. Roll back by reverting
  only this commit.

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


## Native Notification Footer

Upstream appends `INTERNAL_NOTIFICATION_FOOTER` after a framed process result's end marker. Retention removes that exact defining formatter footer before retaining any real human suffix. The async batch delivery invariant exercises both notification-only and notification-plus-human-request turns in `tests/gateway/test_completion_delivery.py`.
