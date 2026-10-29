# Hindsight memory provider

**Patch identity:** `hindsight-retain-visibility`, `evict-memory-shutdown`, and `hindsight-client-selection`.

Soft agent-cache eviction must drain and shut down the agent's memory manager
(including Hindsight's writer and the mem-sync executor), then release clients
off the gateway event loop. Eviction removes the cache entry first, so a refetched
turn builds a fresh manager while retirement completes. Only idle-TTL, LRU-cap
and memory-pressure evictions commit the transcript (`on_session_end`) first;
same-session evictions (`/model`, fallback drift, config signature,
cross-process) never do. Running eviction removes only the cache entry. The existing
turn worker retains the exact agent and soft-releases it after both its blocking
work and the outer turn cleanup have drained, including errors and cancellation.
The worker and turn completion events bind during agent lookup, before publication
in the cache. Completion checks cache identity without removing a successor or
clearing its generation. A per-agent handoff-lock claim prevents duplicate soft
release when idle eviction races turn completion. Idle, LRU-cap and pressure sweeps
record the session-end key on the agent while popping it under the cache lock. When
that agent's turn has not drained, the drain-time release commits with the key, so
the end-of-session commit still runs exactly once. Resolver doubles that reuse an
agent across turns must model cache ownership and match the configured default
model. Otherwise outer-turn cleanup correctly retires their uncached client.
Silence-retry coverage keeps its call-count, retry-budget and history assertions
with that ownership modeled in the shared fixture.
An agent whose `shutdown_memory_provider()` already ran
(`_memory_provider_shutdown`, the `/new` and `/reset` cleanup) is detached without
a second commit or shutdown: `shutdown_memory_provider()` sets that flag and
captures the manager under a per-agent handoff lock (`memory_handoff_lock`) that
eviction's detach also takes, so exactly one side owns `shutdown_all()`. The
lock covers only those reads and writes. The captured local means a cleanup
that outlived its 30s reset timeout still finishes with
`shutdown_all()` after eviction nulls the attribute. `MemoryManager.shutdown_all()`
is idempotent, so racing teardown paths retire providers once. A memory-pressure
batch commits, detaches the manager, and calls `release_clients()` inline, then
runs the deferred `shutdown_all()` on parallel daemon threads, so `malloc_trim` is
not held behind their bounded joins. Releasing the agent's clients first is safe
because no provider drain uses them: Hindsight's writer and shutdown use the
provider's own `_client`, and query rewrite goes through the process-wide
auxiliary client.
Hindsight prefetch keeps one in-flight worker and one latest-wins pending query;
discard and shutdown fence both the pending query and the worker's publication,
and a `BaseException` leaving the worker clears its running flag.
Proof: `scripts/run_tests.sh tests/gateway/test_agent_cache_memory_lifetime.py tests/gateway/test_running_agent_eviction_memory.py`
and `scripts/run_tests.sh tests/plugins/memory/test_hindsight_prefetch_lifetime.py`.
The running-eviction regression traverses model/fallback selection and the production
turn worker with real manager, provider-writer and mem-sync thread identities. It
proves those isolated resources terminate, not a match to live gateway objects.
Retire when upstream soft eviction retires memory providers and Hindsight
prefetch is single-flight. Roll back by reverting the commits carrying
`Fork-Patch: evict-memory-shutdown`; evicted agents then keep their writer and
mem-sync threads until process exit again.

Accepted asynchronous retain operations remain tracked after the first prefetch
visibility wait expires. Later prefetches do not repeat that wait. The existing
writer also ages out initial waits while idle, covering tools-only, synchronous
recall, disabled prefetch waits, and the final turn before idle. It polls late
operations with a two-second pass budget and
five-second cadence. A single operation map keeps each operation's bank, expiry
and whether its initial wait is still due. Tracking is limited to 256 operations
and one hour, with visible warnings on eviction, expiry and terminal failure.
Idle polling cannot release an active prefetch barrier or a configured unlimited
visibility wait. Capacity eviction uses expiry order independently of poll rotation.
Completed and failed operations are removed. Slow status requests rotate to the
back so they cannot starve later operations. Proof:
`scripts/run_tests.sh tests/plugins/memory/test_hindsight_provider.py`.
Retire when the selected upstream integration preserves late visibility without
repeating per-turn waits. Revert the `hindsight-retain-visibility` commit to
restore deadline eviction. No accepted server operation is cancelled on rollback.

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
  gates call `agent.synthetic_prompt.human_prompt_text`. A runtime-owned `display_kind` or an
  unattended platform is authoritative: the whole turn is generated and no text boundary overrides
  it, so text the gateway merged into an internal notification is answered but not used for memory
  (accepted cost of never letting a forged boundary in a payload win). Without that provenance (goal,
  kanban and `/loop` prompts the gateway queues as ordinary events, legacy rows) each producer's own
  formatter boundary decides, keeping any human text merged after it. Add a new generated prompt there, beside its producer constant,
  rather than in a provider. `memory.recall_synthetic_turns` (default off) restores recall on
  generated turns. Proof: `tests/agent/test_synthetic_prompt.py`.
- Automatic recall also excludes producer-owned inline document bodies and channel backfill.
  All text-document producers use `agent.attachment_context.format_document_content`, whose
  character count frames the existing filename/content injection without altering file text.
  Resolve the last channel boundary before accepting document counts, so a frame-shaped line
  in another speaker's backfill cannot consume the current message. Only start-of-message or
  blank-line-separated document frames are producer positions, including the gateway's
  optional current-speaker prefix. A channel header inside a document is not a prelude. Extract those frames before
  trimming whitespace or interpreting generated-prompt boundaries. Unframed legacy document
  text has no provable end and is preserved rather than guessed. The shared channel marker
  registry also owns Discord's unverified notice and reply-only context, Slack's trusted and
  unverified thread headers, and the relay recent-history header. Channel history ends at its
  last `[New message]` boundary, accepting the same quoted-boundary limitation as generated
  templates. Model input and retention keep the complete file and channel content.
  Proof: `tests/agent/test_document_channel_recall_query.py` and the platform document suites.
  Retire when upstream carries the human caption separately through all attachment producers.
- Recall-note peeling preserves the original leftmost regex choice and image-description fallback,
  including notes that become complete across a deletion seam. `agent.recall_context` compiles only
  the existing recall regex subset into ordered states. A literal-opener regex derived from
  that registry skips bracket-dense benign text without per-character state. Complete notes
  use the original regex matches and a single join, with the post-deletion stripped EOF.
  A failed opener or a possible deletion seam falls back to the bounded engine on the original
  input. Memoized lookahead evaluates each
  `(state, input offset)` at most once for each of the 2 possible EOF positions, and retained-prefix
  snapshots avoid restarting at the beginning after deletion. This bounds work and space linearly
  in input length for the fixed registry. The compiler uses CPython's internal regex parser, so
  qualifying Python-version upgrades must run the corpus against the original eraser. Unsupported
  regex constructs fail explicitly rather than silently changing the format contract.
  Proof: `tests/agent/test_recall_context_scanning.py` compares producer, malformed, nested/seam,
  escaped-quote, EOF, and whitespace cases, checks adversarial execution-step growth by input
  size, and rejects bounded-state allocation on 1 MB of bracket-dense JSON with or without a note.
  Retire when upstream has equivalent bounded peeling with the same semantics.
- A generated-only turn also excludes its assistant reply from automatic transcript retention.
  Filtering only the user half creates an orphan assistant result that can turn test, relay or
  recall feedback into apparent personal evidence. Reuse the shared human-part classifier, not
  result-wording rules. Preserve assistant context when a real human suffix survives, and do not
  drop ordinary assistant-only or recalled-context-only turns without generated provenance.
  Proof: `tests/plugins/memory/test_hindsight_generated_turns.py` and the existing provider suite.
  Retire when the selected upstream provider applies the same whole-turn exclusion. Revert only
  that patch to restore user-side-only filtering.
- First-person auto-recall (`recall_owner_subject`, default empty = off). "What car do I drive"
  names no subject, so the owner's personal facts never enter Hindsight's candidate pool: the
  controlled traces show the targets absent before fusion, and no budget, recency or reranker
  setting reorders what was never retrieved. When the owner name is configured and the query has
  I/me/my/mine/we/our/ours/us (case-sensitive `us`, so the country "US" does not count) and does
  not already name the owner, recall runs the user's words and `<words> (<owner>)` and merges the
  two lists round-robin with dedupe, capped at one `recall_max_tokens` budget. Merging, not
  replacing, is required: on the optimization holdout (30 attributes x 2 rounds, 2 request shapes)
  appending alone gained 13-14 personal hits but lost Toyota, birthday and anniversary and pushed
  generic answers out of non-personal first-person queries. Round-robin kept every gain with 0
  losses and 60-64% of the original top 5. Only automatic recall changes. The `hindsight_recall`
  tool keeps the model's own query. Proof: `TestOwnerSubjectRecall` in
  `tests/plugins/memory/test_hindsight_provider.py`. Retire when Hindsight resolves first-person
  queries to the bank owner server-side.
- Template boundary rule: when a template's closing paragraph appears more than once, the LAST
  copy is the generated boundary. Every template is tried and the FURTHEST boundary wins, because
  templates share openings and a payload can hold a complete sibling template (a goal quoting a
  contract continuation) whose boundary ends inside the outer payload. Generated text is never classified as human, which is #320's
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
  covered. A discard must also stop a worker already in flight from republishing, so the manager,
  Hindsight, RetainDB and Honcho share `agent.memory_provider.PrefetchGeneration`: each request
  takes a token, a discard or newer request obsoletes it, and a worker publishes only while its
  token is current, checked under the provider's existing state lock. Hindsight drops its buffered
  recall, RetainDB its three caches, Honcho its pending dialectic plus any cadence or backoff update
  from the obsolete run. Honcho's base context (representation and card) is outside this bound: it
  is a per-session cache injected every turn by design, and a pending refresh only replaces it, so
  dropping the refresh would keep an older copy rather than none. Mem0 keys its buffer on the query and OpenViking,
  ByteRover, Holographic and Supermemory recall live, so they keep the no-op default. The buffer
  lives on the provider instance owned by one agent's manager (each `load_memory_provider` call
  builds a new instance), so it does not cross sessions, and Hindsight's `on_session_switch`
  already drops it on /new, /resume, /branch and compression. In a shared chat it can carry one
  participant's recall to another's next turn, which every async turn already did before this gate. Proof:
  `tests/agent/test_synthetic_prompt.py` (`test_buffered_recall_*`).

## Client Dependency Selection And Passive Readiness

The configured bundled provider must join PM's complete profile dependency union
without a second copy under the home plugin directory. Both selection gates need
the bundled path: `pm.plugins_state._provider_from_config` admits it, and
`pm.workspace.enabled_plugin_entries` resolves it before a same-name user copy.
Update conflict recovery seats bundled providers first and never clears their
`memory.provider` setting. A release-incompatible bundled provider fails before
publishing a new dependency generation or changing profile selections.

These PM changes and the first-generation/conflict regressions are adapted from
[PR #564](https://github.com/0xble/hermes-agent/pull/564), exact head
`37e7a37434beb6d4f80a5d8761243f439df52ab7`. This incident repair does not merge,
rebase, or authorize activation of that branch. It adds the separate status fix:
Hindsight availability checks the same client version/import prerequisite as its
operation factory in every mode. `hermes memory status` prints the actionable
`unavailable_reason` instead of claiming an SDK problem is a missing API key.
Availability is local readiness, not server reachability or a recall test. Status
retains its existing exit contract and performs no new client construction,
provider initialization, dependency installation, or memory API requests. Normal
CLI home/registry bootstrap and auth read locks retain their existing behavior.

Upstream governing guidance was checked at
`NousResearch/hermes-agent@9976f4857b72439d79016354d7b6e1a3ee952e2f`:
`AGENTS.md` and `plugins/AGENTS.md` require a verified premise, an existing
extension point, passive availability, and bounded dependencies. Upstream
[#143970](https://github.com/NousResearch/hermes-agent/issues/143970) reports the
related missing-client symptom. [#144642](https://github.com/NousResearch/hermes-agent/issues/144642)
is related Windows import behavior. Neither establishes this exact PM path bug.

The immutable-release boundary already carries declared extras from the previous
release's committed PM generation, not just its base `.venv`. The `hindsight`
extra is declared in this source, so the recorded recovery selection survives
`prepare_venv`. A ready candidate built by older code uses `carry_active_extras`
from the candidate's own code before activation. The existing tests in
`tests/hermes_cli/test_immutable_releases.py` cover build-time carry, old-builder
ready-candidate carry, and selecting the committed-generation smoke interpreter.
No separate extras framework or release activation path is added here. Subsequent
PM rebuilds are protected by the configured-provider union fix, independently of
the retained extra. Release integration and promotion remain with Maintenance.

Additional isolated proof lives in
`tests/pm/test_bundled_memory_selection.py` (real offline union build with a named
profile and recorded extra, including same-name shadowing) and
`tests/hermes_cli/test_memory_status_client_dependency.py` (all client modes,
missing metadata, unsupported versions, failed import, and supported client).
The status regressions reject network access, SDK construction, initialization,
and dependency preparation and preserve the initialized fixture home's files.

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
  `memory-note-not-authoritative`, `synthetic-prompt-memory-gate`,
  `hindsight-generated-turn-retention`, `hindsight-owner-subject-recall`. Local narrow patches
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
  sites and turn provenance live in `agent/turn_context.py` and `run_agent.py`. Automatic
  recall peels attachment and reply context before applying the provider's query cap.
  The gateway JSON-quotes complete reply text so embedded quotes and paragraph breaks
  cannot impersonate the wrapper boundary. Native image turns use stored caption-only
  text instead of their vision-enriched API copy. Proof: the producer-composed cases in
  `tests/agent/test_attachment_recall_query.py` and full-quote round trips in
  `tests/gateway/test_reply_to_injection.py`. No
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
