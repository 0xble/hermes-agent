# Hermes fork maintenance

## Background

Canonical source: `/Users/brianle/Repos/hermes-agent`, published as
[`0xble/hermes-agent`](https://github.com/0xble/hermes-agent), branch `main`.
Upstream is [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent),
default branch `main`, remote `upstream-live`. Accepted release baseline:
`v0.22.5`, `bf0203a794a07aac3ff764972ab9087baa4e4f4e`.
The prior baselines `v0.22.2` (`d923a5eb487a17c0a68801bba9ef0f56c81040a8`) and
`v0.22.0` (`e06df9c2eae6901f4c88f464e0ae6e753c0428b8`) remain ancestors. The earlier `v0.21.6` baseline
(`818c13be1dc4fd28987e1e881a9408224afd4535`) also remains an ancestor.
Brian waived only the 24-hour release-age wait for `v0.21.6` on 2026-10-08;
every other sync gate applied. The earlier baseline,
`v2026.9.24 + qualified checkpoint` (`eb8d21f482142c236550762ebdb5df4004c39696`),
remains an ancestor. Brian authorized that newer stable pre-tip checkpoint on 2026-09-30 for one
sync and personal-runtime promotion. This selected checkpoint retains prior
`ca6782850432927f33df4775cb6dd45bb51460d2` and released baseline
`v2026.9.24` (`f97608f178d1ffeca59860195ab7da295f7c8e5f`) as ancestors.
Upstream CI run `36801643187` succeeded in every substantive lane, with no
cancelled lanes. The selected revision was 86 commits before frozen upstream
tip `484ebdf16a5127894f16ba883e9630968f945c5e`. Fork qualification and
independent exact-head review remain required before promotion.
This one-run non-release exception does not change recurring release selection
or waive fork tests, independent review, protected landing, or backup safeguards.

Brian separately accepted the *historical* protected-landing bypass for
[PR #135](https://github.com/0xble/hermes-agent/pull/135) on 2026-09-26.
That PR merged at 2026-09-25T09:59:40Z before its required App 15368
`qualification` check completed successfully at 2026-09-25T10:03:49Z on
exactly the merged head `a0da116ac96ab9c14debbce8b7d2a85c8f0430cb`
(merge commit `6e3f8b41ff1f803d6d3147096afac9f3a9ee2127`). This resolves
only the acceptance of that already-landed bypass. It does not retroactively
make the merge protected, waive future required checks, authorize another
admin bypass, or qualify any later `origin/main` candidate; each later
candidate needs its own exact-revision evidence before protected landing.

This replacement fork was established on 2026-09-19. The former fork is preserved
as `0xble/hermes-agent-archived`; its history is not the replacement's baseline.

## Preserve

- Maintain `main` as an upstream **release tag plus narrow, justified patches**.
  This is the accepted migration design's exception to default-branch tracking.
  Inspect upstream `main` for fixes, but do not silently adopt it as the baseline.
  The exact Background exception is authorized, not permission to follow moving main.
- Never reset, downgrade, or reintegrate a release already contained in a validated
  ahead-of-release fork. Preserve published fork commits and upstream ancestry.
  After the required fresh source checks, an unchanged ahead-of-release run is
  a silent no-op. Failed validation or an uncontained newer release is not a no-op.
- Keep profile routing, credentials, Hindsight banks, and browser identities
  separate. Personal plugin and scheduling policy does not override company overlays.
- Keep upstream delegation and update machinery as the owners of their lifecycles.
  Local plugins extend those boundaries rather than restore the retired fork's machinery.
- Source checkout, installed personal checkout, and signed company releases are
  distinct identities. A source merge is not deployment or acceptance evidence.

## Maintenance units

| Unit | Required behavior | Load when | Contract |
|---|---|---|---|
| Completed Responses images | Completed hosted image results become cached assistant media or truthful attachment failures, not empty turns | Codex Responses normalization, provider media caching, or image-only final responses | [Completed Responses images](maintenance/codex-native-images.md) |
| Reaction tool name | Expose `react` with a thumbs-up progress icon, retaining silent rendering of saved legacy calls | Reaction tool registration, progress identity or desktop classification changes | [Reaction tool name](maintenance/react-tool-name.md) |
| CLI streaming preview framing | Keep pending-answer previews distinct from committed transcript while preserving activity chrome | Modern-composer status rendering, streaming previews, or resize-scrollback changes | [CLI streaming preview framing](maintenance/cli-stream-preview.md) |
| Compaction display-fold writer cost | Reuse snapshot identities without historical metadata rereads, preserve live presentation and stale-row invalidation | Display-fold planning, reconciliation or timing changes | [Compaction display fold](maintenance/compaction-write-amplification.md) |
| Browser vault alias authorization | Retain alias refusals independently of prompt preferences and revalidate aliases before resolution and injection | Alias consent, vault fill, or OTP authorization changes | [Browser vault alias authorization](maintenance/browser-vault-alias-authorization.md) |
| Busy turn queue | Preserve admitted user input across same-key generation overlap and session-lease contention | Gateway lease waits, generation cleanup or pending-input recovery changes | [Busy turn queue](maintenance/busy-turn-queue.md) |
| Delivered steer observer | Give native mid-turn insertions a stable plugin delivery identity | Steer insertion, turn identity or plugin lifecycle changes | [Steer delivery observer](maintenance/steer-delivery-observer.md) |
| Turn-origin provenance | Give plugins and session context a default-deny per-turn provenance value | Gateway turn identity, plugin hook context, or session environment changes | [Turn-origin provenance](maintenance/turn-origin-provenance.md) |
| Fork CI | Reproducible patch proof surfaces, hermetic Git fixtures, and complete-suite access on bounded runners | CI, test harness, Git fixture, or proof-surface changes | [Fork CI](maintenance/fork-ci.md) |
| CI toolchain pinning | Resolve local gate uv and Node binaries from the exact repository pins without changing hosted runner semantics | `scripts/ci/toolchain.json`, `scripts/ci/portable.py`, or local gate tool resolution changes | [CI toolchain pinning](maintenance/ci-toolchain-pin.md) |
| Launchd account home | Keep service labels and plist placement on the same real account identity even when process HOME changes | Service naming, sandbox gateway startup or launchd profile ownership | [Launchd account home](maintenance/launchd-account-home.md) |
| Build store dependencies | Resolve package hook dependencies from the caller-owned build store, with scoped cleanup and ordinary sealed-store fallback | PM install lifecycle, dependency lookup, archived web builds, or ARM64 Windows ripgrep staging | [Build store dependencies](maintenance/build-store-dependencies.md) |
| Documents extra | Opt-in, platform-gated document toolchain extra that bundles never preinstall and PM detects once installed | Optional extras, opt-in or platform gates, extra anchors, or document toolchain pins | [Documents extra](maintenance/documents-extra.md) |
| TTS discovery without installation | Defer optional SDK installation until TTS use instead of blocking unrelated turns | TTS capability registration, lazy SDK imports, or tool-schema discovery changes | [TTS discovery](maintenance/tts-discovery-no-install.md) |
| KittenTTS wheel integrity | Keep the optional KittenTTS wheel URL pinned to its verified SHA-256 while preserving platform gates and the lock | KittenTTS dependency URL, wheel hash, platform marker, or lock refresh changes | [KittenTTS wheel integrity](maintenance/kittentts-wheel-integrity.md) |
| Autonomous goals | Opt-in contract-preserving agent controls, recorded completion proof, timed blocker recovery and profile-scoped quiet notices | Goal tools, judging, revisions, approval or idle wake changes | [Autonomous goals](maintenance/goal-autonomy.md) |
| Goal lifecycle | Complete judge criteria, conversational recovery of blocker pauses, and durable command authority during judging | Goal judging, admission, continuation, or concurrent goal-command changes | [Goal lifecycle](maintenance/goal-lifecycle.md) |
| Loop lifecycle | Versioned agent-authorized /loop revisions preserve user authority, cadence state, and cross-surface wakeup persistence | LoopManager revisions, loop wakeup prompts, stale loop-manager caches, or loop receipts | [Loop lifecycle](maintenance/loop-lifecycle.md) |
| Loop completion display | Hide trailing top-level `LOOP_COMPLETE` control text on gateway, TUI, and CLI surfaces while preserving raw loop-stop detection | Loop completion marker filtering, streaming holds, or cross-surface display regressions | [Loop completion display](maintenance/loop-complete-display.md) |
| Compression-deferred turns | Retain human and gateway inputs silently across admission and hygiene turn-hold, then retry after committed compression | Gateway compression admission, hygiene turn-hold, pending-input draining, or compression locks | [Compression-deferred turns](maintenance/compression-lock-deferred.md) |
| Internal notification silence | Internal process/delegation turns use an exact silence contract; parked-goal notices are durable and state-change deduplicated | Internal notification footer, process/delegation wake delivery, parked-goal status notices, or their regressions | [Internal notification silence](maintenance/internal-notification-silence.md) |
| Gateway human-turn silence re-prompt | Human-addressed gateway turns get one fresh gateway continuation after a rejected bare `NO_REPLY`/`[SILENT]`; retry guidance stays API-only, machinery silence and the second-marker fallback remain unchanged | Gateway recursive followups, synthetic provenance, end-of-turn stream filtering, or human-turn silence regressions | [Gateway human-turn silence re-prompt](maintenance/gateway-silence-reprompt.md) |
| Goal status flood retry | Meaningful outcomes/control receipts are quota-protected; routine notices stay expendable; bounded flood retry does not block the turn pipeline | Goal status notice delivery, flood-control classification, or shared cron/notice retry budgets | [Goal status flood retry](maintenance/goal-notice-flood-retry.md) |
| External-wait goal backoff | Back off external/no-progress waits, re-arm live barriers, and keep unchanged silent re-parks and age-only rechecks quiet | Goal judge WAIT semantics, persistent no-progress state, barrier liveness, or idle wake behavior | [External-wait goal backoff](maintenance/goal-external-wait-backoff.md) |
| Auto-recovery ladder notice buffering | Keep post-exhaustion provider countdown notices non-durable during recovery, flush them only on terminal failure, and preserve live wait/interrupt behavior | Auto-recovery ladder notices, retry-status buffering, or recovery-surface regressions | [Auto-recovery ladder notice buffering](maintenance/auto-recovery-ladder-notice.md) |
| Parked goal idle wake | A parked goal resumes when its wait ends, even when no completion turn arrives (restart-killed process, no notify, elapsed timer), and stale restart-resume markers cannot wedge it forever | Goal wait barriers, the gateway loop wakeup watcher, TUI notification poller, or restart process cleanup changes | [Parked goal idle wake](maintenance/goal-parked-idle-wake.md) |
| Restart-parked goal wake | A stale `resume_pending` marker yields ownership to the idle goal ticker after restart auto-resume's freshness window, with CAS fencing against a refreshed marker | Restart-interrupted parked goals and gateway idle wake ownership | [Restart-parked goal wake](maintenance/goal-restart-parked-wake.md) |
| Slack native Stop | Route native session-stop events through the authorized thread-scoped gateway stop path and subscribe Assistant and Agent apps | Slack session lifecycle, native task cards, or manifest events | [Slack native Stop](maintenance/slack-native-stop.md) |
| Gateway stop stays stopped | `/stop` pauses the standing goal and holds the completions it produced until the user's next turn | Gateway `/stop`, completion injection, or goal pause/revival changes | [Gateway stop stays stopped](maintenance/gateway-stop-stays-stopped.md) |
| Command replies keep paths as text | Slash-command replies never auto-upload local files they mention, such as a goal's handoff path | Slash dispatch, `EphemeralReply`/`CommandReply`, or bare-path delivery changes | [Command reply paths](maintenance/command-reply-bare-paths.md) |
| English auto-image descriptions | Keep automatic image descriptions in English while preserving original-language quotes and identifiers | Gateway image-enrichment prompt or vision call-boundary changes | [Auto-image descriptions](maintenance/auto-image-description.md) |
| Telegram rendering | Preserve rich mode selection and prompt/delivery agreement | Telegram rendering changes and every upstream sync; also load runtime ownership before promotion | [Telegram rendering](maintenance/telegram-rendering.md) |
| Session-link rendering | Keep internal session references on Desktop only while preserving Telegram link-target safety | Session-search result or platform dispatch changes | [Session-link rendering](maintenance/session-link-rendering.md) |
| Session-search ranking | Keep BM25 relevance primary while temporal intent remains a deterministic tie-breaker | Relevance, temporal ordering, or result-selection changes in session-search discovery | [Session-search ranking](maintenance/session-search-ranking.md) |
| Session-search isolation | Recall uses readers only, surfaces database failures, and reports monotonic stage timings | Search reads, reader admission, hydration, or search diagnostics changes; also load ranking before ordering changes | [Session-search isolation](maintenance/session-search-isolation.md) |
| Telegram inbound Rich Messages | Formatted pastes reach the agent as Markdown; unclaimed message families are logged, not dropped | Telegram handler registration, inbound classification, mention sources, or reply context changes | [Telegram inbound Rich Messages](maintenance/telegram-inbound-rich-messages.md) |
| Telegram topic titles and icons | Preserve configurable title generation, semantic Bot API topic icons, and duplicate visible labels via lineage aliases | Topic title/icon changes and every upstream sync touching title, Telegram, or session state | [Telegram topic titles and icons](maintenance/telegram-topics.md) |
| Link-informed session titles | The first link's page title/description informs the model title under strict, fail-soft fetch limits | Title input, title prompt, `url_safety` private-address, or link-fetch changes | [Link-informed titles](maintenance/title-link-context.md) |
| Same-session compression recovery design | Design-only contract for a future same-session, lossless compaction path; current runtime still auto-resets after compression exhaustion | Preflight admission, hard-window compaction, queue spooling, compression-exhaustion gateway handling, or this design record | [Same-session compression recovery](maintenance/session-compression-recovery.md) |
| CommandCode provider key alias | Preserve the plugin's `COMMANDCODE_API_KEY` contract when models.dev advertises `CMD_API_KEY`, including the catalog-hit merge path | CommandCode provider resolution, models.dev credential metadata, provider overlays, or picker/runtime credential lookup | [CommandCode provider key alias](maintenance/commandcode-key-alias.md) |
| Copilot ACP usage-less compaction | ACP responses report unknown usage, never fabricated zeros, so estimate-driven compaction still fires | Copilot ACP client response or usage accounting changes | [Copilot ACP usage](maintenance/copilot-acp-usage.md) |
| Nightly concurrency and compaction regressions | Preserve early process output, tolerate concurrent DB quarantine, and exercise actual ACP summarization | Process heartbeat, state DB preflight/quarantine, or ACP compaction E2E changes | [Nightly regression repairs](maintenance/nightly-regression-0926b.md) |
| Foreground command exit cleanup | Fence graceful exit against spawning foreground commands so no child outlives its host | Foreground spawn publication, terminal environment process-exit cleanup, or live foreground killing changes | [Foreground exit cleanup](maintenance/foreground-exit-cleanup.md) |
| Agent wait ergonomics | Bound default process waits and keep unchanged delegation lists from consuming turns | Process wait defaults, timeout guidance, or delegation list observations; load async delegation capacity for queued-state changes | [Agent wait ergonomics](maintenance/agent-wait-ergonomics.md) |
| Terminal heavy-slot | Heavy terminal launchers share machine-wide slots; test, workspace, and PM workers default to four per job | Terminal execution/classification or test, workspace-check, PM build/install/bytecode parallelism changes | [Terminal heavy-slot](maintenance/terminal-heavy-slot.md) |
| Terminal spill prune throttle | Spill-directory pruning is single-flight, off the call thread, and at most once per minute per directory, with unchanged 7-day retention | Terminal spill creation, pruning, or retention changes | [Terminal spill prune throttle](maintenance/terminal-spill-prune.md) |
| Terminal PATH cache | Terminal commands reuse one per-process PATH composition; PM writes and a 30 s TTL refresh it, activation stays live | `pm.user_facing`, `pm.install` PATH composition, local run-env construction or PM durable writes | [Terminal PATH cache](maintenance/terminal-path-cache.md) |
| Snapshot keeps shell functions | Functions and aliases survive every command of a terminal session, not just the first | Terminal session snapshot bootstrap or per-command re-dump changes | [Snapshot functions](maintenance/snapshot-keep-functions.md) |
| Explicit topic title receipts | Report the stored alias and actual Bot API rename result | `/title` Telegram topic changes and upstream title sync | [Explicit title receipts](maintenance/telegram-title-receipts.md) |
| Cron fallback routing | Keep scheduled agents' backup chain independent of interactive routing | Changing cron/provider resolution or evaluating an upstream release | [Cron fallback routing](maintenance/cron-fallback-routing.md) |
| Cron session persistence | Require a session-store handle before agent jobs consume gate or repeat state | Cron session acquisition, pre-run gates or pre-start failure bookkeeping | [Cron session persistence](maintenance/cron-session-persistence.md) |
| Reviewed cron delivery | Hold opted-in local reports for invisible topic review, bounded rerun, and contract-checked bot presentation | Cron delivery, internal capture, review contracts, topic resolution, or upstream sync touching those paths | [Reviewed cron delivery](maintenance/reviewed-cron-delivery.md) |
| Cron in-flight cancellation | Immediately interrupt local remove/pause runs and expose running/side-effect uncertainty | Cron mutation, cancellation or running-status changes | [Cron in-flight cancellation](maintenance/cron-inflight-cancellation.md) |
| Cron prompt-update guard | Refuse placeholder and >90% shrink prompt updates from the agent tool and CLI unless `allow_shrink`/`--allow-shrink` is set | Cron update/edit prompt paths or the `cronjob_manage` schema change | [Cron prompt-update guard](maintenance/cron-prompt-update-guard.md) |
| Cron admission | Protect short-interval worker starts with earlier eligibility and 1 extra reserved worker, without changing the general FIFO cap or process-exit fence | Cron completion rearm, built-in ticker waits, or executor admission changes | [Cron admission](maintenance/cron-admission.md) |
| Cron restart survival | Keep launchd-managed macOS cron workers alive through gateway process-group termination | Cron external worker dispatch, liveness recovery, or launchd restart changes | [Cron restart survival](maintenance/cron-restart-survival.md) |
| Immutable releases S2 | Pin code and venv to immutable releases behind an atomic pointer; preserve migration and rollback receipts | Update, release staging, launchd path resolution, cron worker pins or retention changes | [Immutable releases and activation runbook](maintenance/seamless-restart-s2.md) |
| Release extras carry | New releases carry the active release's committed extras, at build time and by a PM commit into a ready candidate before activation | Release venv extras selection, PM generation layout, or `release-layout.json` source interpreter changes | [Release extras carry](maintenance/release-extras-carry.md) |
| Telegram ingress non-blocking | The update consumer never waits on outbound pacing, the pending-update probe requires no dispatch progress, and reconnect waits for the old poller to release | Telegram update handlers, busy or inline command replies, the heartbeat pending probe, or poller token ownership | [Telegram ingress non-blocking](maintenance/telegram-ingress-nonblocking.md) |
| Telegram first-poll health | Each new polling generation proves health from a non-blocking first getUpdates; steady-state polls keep the long poll | The instrumented getUpdates request, polling-generation fencing, `start_polling` arguments, or a PTB upgrade | [Telegram first-poll health](maintenance/telegram-first-poll-health.md) |
| Stdio wrapper chain | Agent builds and thread-scoped silencing never stack or loop `sys.stdout`/`sys.stderr` wrappers, and wrapper attribute lookup cannot recurse | `_SafeWriter`, `_install_safe_stdio`, `thread_scoped_output`, or other process-lifetime stdio rebinding | [Stdio wrapper chain](maintenance/stdio-wrapper-chain.md) |
| Telegram delivery | Preserve flood coherence, split-send recovery, and legacy emphasis | Telegram send/edit/typing, delivery ledger, or emphasis changes | [Telegram delivery](maintenance/telegram-delivery.md) |
| Stream preview crash ownership | Resume into the exact prior editable platform message, never a reset or successor preview | Stream send/edit journaling or crash auto-resume ownership changes | [Stream preview crash ownership](maintenance/stream-crash-visible-persist.md) |
| Thread status continuity | Keep typing pauses, phrases and cleanup scoped to one thread while preserving legacy adapter signatures | Shared typing state, Slack status continuity, or typing pause/resume callers | [Thread status continuity](maintenance/thread-status-continuity.md) |
| Telegram typing refresh | Keep Telegram's 4.5s refresh deadline-based and defer one coalesced trailing re-arm without extending it past final delivery | Telegram typing cadence, post-send re-arm, or typing-refresh regressions | [Telegram typing refresh](maintenance/telegram-typing-refresh.md) |
| Telegram stale final delivery | Recover completed replies from deleted private DM topics without duplicating partially sent content or moving interim output | Telegram private-topic final sends and recovery | [Telegram stale final delivery](maintenance/telegram-stale-final-delivery.md) |
| Telegram internal delivery recovery | Redeliver failed answers on same-adapter polling recovery without crossing profile ownership | Polling health, final delivery settlement, or runtime ledger replay | [Telegram internal delivery recovery](maintenance/telegram-internal-delivery-recovery.md) |
| Status bubble ownership and cleanup | Keep Telegram topic/turn statuses independent and forget deleted bubbles | Telegram/Slack `send_or_update_status`, `delete_message`, or progress cleanup changes | [Status cache after cleanup](maintenance/status-cache-after-cleanup.md) |
| Per-turn progress cleanup | Keep queued callback ownership isolated and await progress deletion before the next turn | Post-delivery callback registration, delivery handoff, or progress cleanup changes | [Per-turn progress cleanup](maintenance/progress-cleanup-turns.md) |
| Outbox coalesced sweep | Deferred outbox redelivery runs as one sweep per store and adapter identity, with serialized recovery, closed connections, durable receipts and held rows logged once | Outbox retry scheduling, `recover()`, receipts, held-row reporting or store connections | [Outbox coalesced sweep](maintenance/outbox-coalesced-sweep.md) |
| Async delegation ledger off the loop | Every state.db ledger claim, settle, replay and auto-resume call from gateway async code runs in a worker thread, so a slow WAL checkpoint cannot trip the liveness watchdog | Durable completion claim/settle, startup replay, or auto-resume scheduling from the gateway loop | [Async delegation ledger off the loop](maintenance/async-delegation-ledger-off-loop.md) |
| Async completion source convergence | Distinguish route-free terminal drops from recoverable routing and ledger failures. | Completion routing, watcher retry groups, or boot-notice routing changes. Read ledger ownership first. | [Unit](maintenance/async-completion-source-convergence.md) |
| Gateway loop state.db guard | The event-loop thread never waits on state.db or session-store locks, and a restart cannot start its resume/cron herd at once | Gateway async code touching state.db or the session store, or boot-time concurrency | [Gateway loop state.db guard](maintenance/gateway-loop-statedb-guard.md) |
| State.db prune lock batching | Automatic session retention deletes run only in the deletion window and in short transactions, with actionable long-lock diagnostics | Session auto-prune, retention maintenance, SQLite write-lock diagnostics, or pruning regressions | [State.db prune lock batching](maintenance/state-db-prune-lock-batching.md) |
| Desktop idle session refresh cost | Bound metadata serialization and preview projection without changing polling or session-list semantics | Change watcher signatures, preview SQL or recent session-list queries | [Desktop idle CPU](maintenance/desktop-idle-cpu.md) |
| Desktop source handoff fixture | Correlate the mock's legacy claim with the timestamp passed by the Desktop | Source-handoff fixture or legacy marker correlation changes | [Desktop source handoff fixture](maintenance/desktop-checkout-source-fixture.md) |
| File-tool recovery | Recover malformed regexes transparently and provide resolved missing paths and complete patch ambiguity locations | File/search/patch diagnostics, fuzzy strategies, or upstream file-tool recovery changes | [File-tool recovery](maintenance/file-tool-recovery.md) |
| Bounded filename search | Keep ripgrep filename walks on a small CPU budget without changing content-search parallelism | `search_files(target='files')`, ripgrep command construction, or filename-search regressions | [Bounded filename search](maintenance/bounded-filename-search.md) |
| Busy state.db turn survival (upstream backport) | A SQLite lock on lease refresh is a missed tick bounded by the row's committed expiry, and a failed post-tool flush keeps `session_persistence_failed` instead of becoming `pending_tool_result` | Turn-lease refresh, `refresh_session_turn_lease`, the finalizer's silent-tool-tail branch, or a sync containing upstream #134577 or #132559 | [Busy state.db turn survival](maintenance/state-db-lock-turn-survival.md) |
| Progress bubble flood deferral | A flood-refused progress edit retries later instead of turning every later tool line into its own message | Progress overflow split, send-or-edit tick, or progress edit failure classification changes | [Progress bubble flood deferral](maintenance/progress-overflow-flood-defer.md) |
| Restart continuation | Let interactive platforms continue interrupted work after a gateway restart, with shared bounded replay admission and per-session input ordering | Restart recovery, replay scheduling, resume notes, config bridging, or adapter resume defaults | [Restart continuation](maintenance/restart-continuation.md) |
| Background turn admission | Cap running turns nobody typed (restart resumes, goal/loop wakes, completions) gateway-wide, never gate typed input, and refuse rather than drop queued wakes on drain | Wake admission, restart resume scheduling, the replay scheduler, or `gateway.background_turn_concurrency` | [Background turn admission](maintenance/background-turn-admission.md) |
| Restart notices | Scope shutdown notices to conversation lanes, preserve interim streams, and describe the actual resume policy | Shutdown advisories, home-channel broadcast, or restart notice wording | [Restart notices](maintenance/restart-notices.md) |
| Bounded restart shutdown | Retain unverifiable survivors, signal active workers before scope cleanup, and refresh escaped descendants before escalation | Process-registry bounded shutdown, detached identity, or systemd scope cleanup changes | [Bounded terminal shutdown](maintenance/seamless-restart.md) |
| Restart verification truth | Cover cron drain in the bounded observer wait, preserve outgoing inventory PIDs, and treat saved obligations as history | CLI update/restart wait, fleet snapshot, or pending warning changes | [Restart verification truth](maintenance/restart-verification-truth.md) |
| Delegation restart drain | Planned restarts wait for live background delegations, and interrupted children report why they stopped | Restart wait, shutdown drain accounting, CLI exit-wait budget, or child interrupt reporting | [Delegation restart drain](maintenance/delegation-restart.md) |
| Background process survival | Opted-in local background commands survive gateway restart and deliver one durable completion; cut receipts remain non-final until completion and use a separate retention budget | Background terminal process checkpoints, cut receipts, restart adoption, or completion delivery | [Background process survival](maintenance/background-process-survival.md) |
| Delegation durable retry | Every background delegation ends delivered or durably settled; routine stops stay quiet, only a gone-parent undeliverable result is gateway-sent; transient failures and lost owners retry durably within a 6-attempt/24h lineage budget | Delegation failure classification, retry scheduling, `action='resume'`, retry notices, or `async_delegations` retention | [Delegation durable retry](maintenance/delegation-durable-retry.md) |
| Background delegation restart survival | Completed results win shutdown races; unfinished cut work retains owed delivery/retry, per-child one-shot recovery, and one explicit continuation notice with bounded expiry | Restart-cut lifecycle state, resumable retention, child recovery claims, or cut completion notices | [Background delegation restart survival](maintenance/delegation-restart-survival.md) |
| Delegation routes by parent | Select child routes from the live parent model, preserving pure inheritance, unmatched pinned routes, and internal review routing | `delegation.by_parent`, model preset expansion/persistence, child reasoning, or plugin lifecycle launches | [Delegation routes by parent](maintenance/delegation-routes-by-parent.md) |
| Delegation service tier | Opt-in inheritance of the parent's Fast mode (route-independent, re-derived for the child's route) and of an explicit session reasoning pick, plus the `capabilities.fast_mode` custom-provider opt-in | `delegation.inherit_service_tier`, `capabilities.fast_mode`, `delegation.reasoning_effort`, `agent.reasoning_override` or child runtime/request-override resolution | [Delegation service tier](maintenance/delegation-service-tier.md) |
| Async delegation capacity admission | Async-capable background delegation never falls back to blocking inline execution at pool capacity; bounded pending work is queued, cancellable, routed, and truthfully surfaced on restart | `delegate_task(background=true)`, async admission/capacity, `action='list'`, stop/session teardown, or completion routing changes | [Async delegation capacity admission](maintenance/async-delegation-capacity.md) |
| Bounded request-debug artifacts | Cap each diagnostic JSON and prune old timestamped dumps without touching session artifacts | Request-dump serialization, writes, or retention changes | [Bounded request-debug artifacts](maintenance/bounded-request-dumps.md) |
| Bounded delegation notices | Completion notices echo only a bounded head of the dispatch context | Async-delegation notice rendering changes | [Bounded delegation notices](maintenance/bounded-delegation-notices.md) |
| Plugin-claimed failure notices | A plugin that recovers a child failure (review fallback) can suppress the premature "Subagent failed" notice, decided once per failure | Subagent failure notice or `subagent_failure_notice` hook changes | [Review fallback notice](maintenance/review-fallback-notice.md) |
| Durable delegation terminal fallback | Child-failure notices and terminal results whose lifecycle write failed persist as outbox rows that survive restart, and settle the lifecycle row in the same transaction | Async-delegation outbox events, terminal persistence fallback, startup replay, or session recovery of `async_delegation_events` | [Durable delegation terminal fallback](maintenance/durable-delegation-terminal-fallback.md) |
| Hygiene prompt not reused | A prompt persisted by memory-only gateway hygiene compaction is rebuilt on the next real turn, not adopted as a surface | Stored-prompt restore or hygiene compaction prompt handling | [Hygiene prompt not reused](maintenance/hygiene-prompt-not-reused.md) |
| Hygiene deferred adoption | An adopted hygiene turn-hold is silent and does not re-trigger hygiene; a stale lease-less rewrite cannot re-insert a replaced generation | Turn-hold handling, deferred adoption, the stored hygiene token count, or lease-less `archive_and_compact` callers | [Hygiene deferred adoption](maintenance/hygiene-deferred-adoption.md) |
| Typed messages preempt autonomous turns | In queue mode a typed message redirects (never kills) a goal, resume, completion, heartbeat or relay turn instead of waiting behind it; the user's own turns still queue | Busy-session admission, `busy_input_mode`, goal continuations, boot resume, or relay classification | [Typed messages preempt autonomous turns](maintenance/human-preempts-autonomous.md) |
| Gateway commands while busy | Preserve alias expansion and defer-until-idle on the busy path | Busy-session guards, `quick_commands`, or slash-command admission changes | [Gateway commands](maintenance/gateway-commands.md) |
| Background topic recovery | Keep detached `/bg` and `/btw` answers in recovered Telegram DM topics without cross-topic reply anchors | `/bg`, `/btw`, or Telegram DM-topic source routing changes | [Background topic recovery](maintenance/background-topic-recovery.md) |
| Live gateway inference controls | Apply busy `/reasoning` and `/fast` at the live agent's next model request without eviction | Gateway inference controls, agent request overrides, or cache wiring | [Live inference controls](maintenance/live-inference-controls.md) |
| Attended model switching | Route genuine user activity to the configured attended model while preserving the default route when unset | Attended model configuration, activity clocks, model routing, or attended turn transitions | [Attended model switching](maintenance/attended-model-switching.md) |
| Model presets | Shared flat preset authoring, route/reasoning readers and concrete-selection persistence | Preset/config loaders, model selection writers, or new model config consumers | [Model presets](maintenance/model-presets.md) |
| Session reasoning persistence | Persist session `/reasoning` picks in the routing entry so a gateway restart keeps them, like `/model` pins | Session reasoning override storage, rehydration, or conversation-boundary clears | [Session reasoning persistence](maintenance/session-reasoning-persistence.md) |
| Session-scoped Fast expiry | Switch session `/fast fast` and `/fast ultrafast` overrides to explicit normal after a configurable wall-clock deadline | `agent.fast_expiry_seconds`, session tier resolution, cached-agent request overrides, or Fast status/notice changes | [Session-scoped Fast expiry](maintenance/session-fast-expiry.md) |
| Native Telegram topic tool | Let an agent create or edit a Telegram DM topic and bind its settings, opening brief, and relay address to a native Hermes session | `telegram_topic`, Telegram DM topic creation/editing, topic bindings, or opening-brief delivery | [Native Telegram topic tool](maintenance/telegram-topic-tool.md) |
| Typed session-state inspection | Expose bounded, profile-scoped, read-only Hermes topic, session, and delivery state without generic SQL or transcript leakage | `session_state`, durable session-state readers, toolset registration, or state inspection regressions | [Typed session-state inspection](maintenance/session-state-inspection.md) |
| Queued voice transcription | Transcribe and echo queued voice immediately with bounded concurrency, reuse it at drain | Busy queueing, pending-event STT cache, transcript echo, or drain changes | [Queued voice STT](maintenance/queued-voice-stt.md) |
| Worktree GC Git isolation | Host Git config never hides files from the reclaim safety check; every listed path is archived exactly or the worktree is kept, except regenerable directories, which go with the tree | Worktree-GC dirty checks, archiving, or reclaim changes | [Worktree GC Git isolation](maintenance/worktree-gc-git-isolation.md) |
| Git tmp-pack holder scope | Linux removes aged aborted-transfer packs when only foreign checkouts have Git processes, while target/shared holders and all stale locks remain conservatively protected | `hermes_cli/gitlock.py`, Git process ownership or tmp-pack recovery tests | [Git tmp-pack holder scope](maintenance/gitlock-tmp-pack-holder-scope.md) |
| Camofox accounts and vault | Preserve named accounts, Connect/secret-safe fills, shadow-DOM login forms | Browser account, vault, or 1Password backend changes | [Camofox and vault](maintenance/camofox-vault.md) |
| Camofox navigation titles | Return the exact owned tab's title without crossing account boundaries | Camofox navigation or tab-list changes | [Camofox navigation titles](maintenance/camofox-navigation-titles.md) |
| Camofox tab reuse | Close disposable owned tabs automatically while preserving kept/adopted handoff tabs and live sibling ownership | Camofox cleanup, tab adoption, handoff, leases, or compression task-id changes | [Camofox tab reuse](maintenance/camofox-tab-reuse.md) |
| Browser file transfer | Attach Python-host file bytes and retrieve captured downloads through the owned Camofox tab | Camofox tab actions or upload/download transport changes | [Browser file transfer](maintenance/browser-upload.md) |
| Slack rich-text rendering | Preserve authored list starts and emphasis across opaque inline content | Slack rich-text list parsing, grouping, inline formatting, or outbound Block Kit rendering changes | [Slack rich-text rendering](maintenance/slack-ordered-list-numbering.md) |
| Slack transcript cards | Separate source/speaker context from literal transcript content | Shared STT echo, native Slack text delivery, or upstream transcript hook changes | [Slack transcript cards](maintenance/slack-transcript-cards.md) |
| Slack ephemeral task cards | Native "Hermes is working" cards are sealed then deleted at every turn end, leaving only the reply | Slack native task-card start, reopen, stop, or cleanup changes | [Slack ephemeral task cards](maintenance/slack-ephemeral-task-cards.md) |
| Slack thread root redelivery | Admit user edits, not thread metadata, with a cold cache after restart | Slack changed-message normalization, edit deduplication, or ingress refactoring | [Slack thread root redelivery](maintenance/slack-thread-root-redelivery.md) |
| Slack status on the legacy API | Thread status and its clear stay on `assistant.threads.setStatus`; titles may use Agent Sessions | Slack status/title calls or upstream Agent Sessions changes | [Slack status legacy](maintenance/slack-status-legacy.md) |
| Agent secret entry | The agent may type a self-fetched password or one-time code from an authorized source when the vault has no compatible item or cannot mint the code; values shown on a page or supplied in chat never count, and card numbers/CVCs remain vault-only | Vault tool descriptions, browser input prompt text, computer-use guidance, or upstream vault prompt changes | [Agent secret entry](maintenance/agent-secret-entry.md) |
| Security guidance plugin | Keep bounded path-aware security pattern guidance and explicit warning/block semantics | Security-guidance pattern, plugin wiring, or focused-test changes | [Security guidance plugin](maintenance/security-guidance.md) |
| Essential skill opt-out | Let one home opt out of seeding and protecting the essential `hermes-agent` skill | Bundled or essential skill seeding, disabled-list, or delete-guard changes | [Essential skill opt-out](maintenance/essential-skill-opt-out.md) |
| Background review memory delete | Opt-in plus a current-profile durable journal lets unattended review replace/remove memory; otherwise stage | Background-review memory gate, transaction observers, or `memory` config changes | [Background review memory delete](maintenance/background-review-memory-delete.md) |
| Exact memory context notes | Strip only exact known wrappers, preserving same-prefix recalled facts and adjacent whitespace | Memory note sanitizer or wrapper wording changes | [Exact memory context notes](maintenance/memory-context-note-stripping.md) |
| Background skill review human-turn gate | Automatic skill review accumulates iterations but requires human-authored input since the last review | Automatic skill-review trigger, skill nudge counter, or synthetic-turn provenance changes | [Background skill review human-turn gate](maintenance/skill-review-human-turn.md) |
| Skill observation files | One immutable `<skill>@<suffix>.md` file per observation, indexed and archived independently | Observation store, `hermes observations`, or observation writers change | [Skill observation files](maintenance/skill-observation-files.md) |
| Profile plugins and skills | Keep Hermes runtime contracts and skill ownership boundaries explicit while profile plugin source remains external | Profile-plugin integration, skill guard, or curation changes | [Profile plugins](maintenance/profile-plugins.md) |
| External skill maintenance guidance | Tell agents that `skills.external_dirs` installs are read-only and must be changed at their source, while local skills retain `skill_manage` maintenance guidance | Skill prompt assembly, background review guidance, or external skill write refusals | [External skill maintenance guidance](maintenance/external-skill-guidance.md) |
| Wrapped gateway service ownership | Protect service-owned gateway descendants from updater and reaper manual sweeps | Service PID discovery, updater restart, or wrapper changes | [Wrapped gateway ownership](maintenance/wrapped-gateway-ownership.md) |
| Gateway launchd QoS | Run the macOS gateway LaunchAgent at Interactive (DEFAULT QoS), not launchd's UTILITY default | Gateway plist template or launchd ProcessType changes | [Gateway launchd QoS](maintenance/gateway-launchd-qos.md) |
| Launchd restart verification | Start the respawn window after the old gateway exits, so a slow drain is not a failed restart | Updater launchd restart verification or shutdown budget changes | [Launchd verify after shutdown](maintenance/launchd-verify-after-shutdown.md) |
| Backup, state, and tooling | Truthful backups, schema rehearsal, per-job timezone, fork maintenance scripts | Backup, cron scheduling, context ports, or maintenance script changes | [Backup and tooling](maintenance/backup-and-tooling.md) |
| Quick snapshot recovery | Bound partial captures while retaining verified recovery generations | Quick-snapshot capture, pruning, or restore changes; read Backup and tooling too | [Quick snapshot recovery](maintenance/quick-snapshot-recovery.md) |
| Hindsight memory provider | Keep the provider constructible before `initialize()`, retire memory workers on soft agent eviction, and coalesce Hindsight prefetch | Memory plugin lifecycle, retain strategy, cron-exclusion, agent-cache eviction, or prefetch changes | [Hindsight memory](maintenance/hindsight-memory.md) |
| Auxiliary fallback configuration | Recognize runtime-read fallback chains without config backfill or invalid preset saves | Auxiliary config defaults, fallback chains, or preset reference persistence | [Auxiliary fallback configuration](maintenance/auxiliary-fallback-config.md) |
| Auxiliary overload fallback | Route status-less provider overloads through configured auxiliary fallback | Auxiliary error classification or fallback-chain changes | [Auxiliary overload fallback](maintenance/auxiliary-overload-fallback.md) |
| Auxiliary route health | Share typed, endpoint-scoped auxiliary route health across callers and processes with single-probe recovery and once-per-epoch notices | Auxiliary unhealthy marking, recovery probes, skip notices or route-health state | [Auxiliary route health](maintenance/auxiliary-route-health.md) |
| Compression route deadline | Preserve auxiliary compression fallback at the shared route deadline | Compression worker cancellation, deadline, or fallback changes | [Compression route deadline](maintenance/compression-route-deadline.md) |
| Compression pool saturation | A full compression pool defers instead of spending a compression attempt | Pool admission, transient compression signals, or preflight attempt accounting | [Compression pool saturation](maintenance/compression-pool-saturation.md) |
| Manual compression levels | `/compress --level 1-3` escalation and a `here N` head that keeps no extra tail | `/compress` parsing, `here N`, or manual compression budget changes | [Manual compression levels](maintenance/manual-compression-levels.md) |
| Long request turn split | Split an oversized in-progress turn even when its opening request is long, so goal runs stay compressible | Oversized-turn split gates or in-flight request restatement changes | [Long request turn split](maintenance/long-request-turn-split.md) |
| Oneshot plugin-hook discovery | Complete background plugin discovery before first-turn lifecycle hook delivery so `pre_llm_call` cannot observe a partial registry | Plugin startup discovery barriers, oneshot hook delivery, or plugin registry readiness | [Oneshot plugin-hook discovery](maintenance/oneshot-plugin-hook-discovery.md) |
| Plugin and hook dispatch gates | Keep concurrent lifecycle and nested execute_code hook calls on distinct gate keys, let shell hooks own their matcher, timeout and fail_closed policy, skip inapplicable hooks before forking, back off repeated fail-open timeout/spawn failures, avoid disk-cleanup temp-file races, and log default capability denials at DEBUG | Hook gate identity, shell-hook dispatch/backoff/applicability, execute_code nested tool-call ids, disk-cleanup state writes, or capability-check logging | [Plugin and hook dispatch gates](maintenance/plugin-hook-gates.md) |
| MCP reaper live-process grace | Skip dead PID ledgers, preserve the two-second grace only for verified spawn incarnations | MCP stdio child capture, release, orphan reaping or shutdown changes | [MCP reaper live-process grace](maintenance/mcp-reaper-live-grace.md) |
| MCP process-exit retry guard | Stop MCP transport retries during CPython executor teardown while retaining live executor failures | MCP retry classification, shutdown predicate, or park-warning attribution changes | [MCP process shutdown](maintenance/mcp-process-shutdown.md) |
| MCP caller identity | Opted-in MCP servers receive the calling session's ContextVar identity as per-call request `_meta`, never the model's arguments or `os.environ` | MCP tool-call dispatch, per-server opt-ins, or session identity reads for MCP | [MCP caller identity](maintenance/mcp-caller-identity.md) |
| Bounded macOS sidecar holder scan | Share one bounded foreign descriptor scan while checking self-held retired sidecars on every call | macOS deleted-WAL scans, libproc timeouts, worker cooldown or descriptor identity | [Bounded macOS sidecar holder scan](maintenance/state-db-holder-scan.md) |
| TUI session event publication | Keep replay sequencing and stable per-session mailbox admission atomic across normal and connector publishers | Session fanout, connector ownership, attach/detach, replay, or compute-host drain changes | [TUI session event publication](maintenance/tui-event-publication-order.md) |
| Release defects | Narrow, guarded fixes for defects found while syncing to `v2026.9.24`, each with a patch identity and guard test | Before changing a file a section names, when a sync review finds a defect, or when checking whether upstream now fixes one | [Release defects](maintenance/release-defects.md) |
| Direct web extraction and local docs | Bounded, safe direct fetches and checkout-backed docs avoid paid provider calls | Web extraction routing, URL safety, docs mapping, or extract config changes | [Direct web extraction](maintenance/web-extract-direct.md) |
| Proactive tool-result prune default | Enable the existing deterministic no-LLM tool-result prune at 48000 tokens while protecting the recent tail | `compression.proactive_prune_tokens`, its config/docs, and proactive-prune regressions | [Proactive tool-result prune default](maintenance/proactive-tool-result-prune.md) |
| Agent friction repairs | Preserve unread pending results and session-kernel state/lifetime attribution | Context-pruning dedupe, local/remote kernel lifecycle, attribution, or their regression tests | [Agent friction repairs](maintenance/agent-friction-repairs.md) |
| Secret-safe provider diagnostics | Check the selected custom HTTP endpoint without exposing credentials through argv, responses or logs | Provider diagnostic helper, credential resolution or output/logging changes | [Secret-safe provider diagnostics](maintenance/io-safe-provider-probe.md) |
| Provider-reported cost | Capture a provider's finite per-call billed amount and persist it so every existing reader displays it exactly once, without changing estimated/actual combine semantics | Usage normalization, `estimate_usage_cost`, main-turn cost persistence, or `update_token_counts` cost columns | [Provider-reported cost](maintenance/provider-reported-cost.md) |
| Provider output-cap preflight | Price the final provider wire request and effective output cap before the hard-window gate, shrink only within the current turn, and preserve provider thinking floors | `request_fits`, provider output-cap resolution, preflight ceiling state, or native/tool-schema request sizing | [Provider output-cap preflight](maintenance/provider-output-cap-preflight.md) |
| Deferred local tool_call batching | Multi-entry local `tool_call` batches run losslessly, bounded and interruptible through each entry's ordinary pipeline, with entry-indexed, non-misleading errors | Tool Search bridge, `tool_call` batching, local batch splitter, or connector batch dispatch changes | [Deferred local batch](maintenance/deferred-local-batch.md) |
| Unicode TAG character stripping | Strip invisible plane-14 TAG characters from model-facing skill, web, browser, MCP, and read_file results | Tool-result construction, Unicode tag stripping, or those tool output paths | [Unicode TAG character stripping](maintenance/unicode-tag-strip.md) |

## Active patch record: state.db prune lock batching

- **Patch identity:** `state-prune-lock-batching`.
- **Behavior:** Automatic session retention pruning enforces the configured deletion window inside the maintenance function and at its long-lived callers, so stale or alternate Hermes entrypoints cannot trigger a daytime prune. Inside the window, it deletes sessions and their FTS-triggered messages in short idempotent transactions with guard-safe compression lineage handling, and reports long write-lock holds with callback and pid. Long compaction write holds also attribute preparation, active/carried/tail enumeration, timestamp carry, display-index cursor enumeration/materialization and session-counter updates with attempt-local timings and row counts, including per-query wall/thread-CPU pairs. Non-Linux lock diagnostics no longer claim a holder released just before the deadline when `/proc/locks` is unavailable.
- **Source surfaces:** `gateway/run.py`, `hermes_cli/cli_auto_maintenance.py`, `hermes_state_maintenance.py`, `hermes_state.py`, `hermes_state_lockowners.py`, `hermes_state_messages.py`, `hermes_state_message_archive.py`, `hermes_state_write_metrics.py`, and focused state/gateway/CLI tests.
- **Upstream status:** No equivalent upstream fix for the combined daytime gate, bounded prune transactions, and macOS diagnostic correction.
- **Focused regression:** `uv run --with pytest --with pytest-asyncio pytest -q tests/gateway/test_state_db_maintenance_window.py tests/hermes_cli/test_session_vacuum_config.py tests/hermes_state/test_hermes_state.py tests/hermes_state/test_write_lock_owner_attribution.py tests/hermes_state/test_compaction_write_metrics.py`.
- **Retirement:** Remove when upstream provides equivalent deletion-window gating, bounded guard-safe pruning, and cross-platform long-lock diagnostics.
- **Rollback:** Revert the commit carrying `Fork-Patch: state-prune-lock-batching`.

## Active patch record: relay silence (S1)

- **Patch identity:** `relay-silence`.
- **Behavior:** Treat registered agent-origin headers, beginning with relay's `[relay from=... receipt=... (task=...)?]`, as unaddressed inbound prompts so a bare `NO_REPLY` remains silent; preserve explicit addressed messages.
- **Source surfaces:** `gateway/platforms/base.py` busy admission, `gateway/response_filters.py`, `gateway/run_inbound.py`, `gateway/run_busy.py`, locale `gateway.errors.unexpected_silence` strings, and gateway silence/response-filter/busy regression tests.
- **Upstream status:** NousResearch #117354 covers group chats only; no relay-equivalent DM behavior.
- **Focused regression:** `scripts/run_tests.sh tests/gateway/test_response_filters.py tests/gateway/test_gateway_silence_tokens.py tests/gateway/test_busy_relay_silence.py tests/gateway/test_busy_redirect_anchor.py`.
- **Retirement:** Remove this patch when upstream supports equivalent agent-origin silence behavior across normal, queued, and recovery paths.
- **Rollback:** Revert the relay-silence fix commit.

## Active Patch Record: Requested And Served Speed Tiers

- **Patch identity:** `speed-tier-audit`.
- **Behavior:** One `speed tier:` INFO record per completed Codex Responses or Chat Completions response, including when neither side reports a tier. Logs negotiated request kwargs and the response echo, with missing echoes labeled `unreported`. An Ultrafast parent passes the tier only to an eligible child route, using the child's credential. Explicit delegation overrides win and unsupported children get no inherited paid tier.
- **Source surfaces:** `agent/turn_api_call.py`, `agent/relay_llm.py` provider-request capture, `agent/turn_usage.py`, chat streaming accumulators, `tools/delegate_tool_config.py`, `tools/delegate_tool.py`, and [Requested And Served Speed Tiers](maintenance/speed-tier-audit.md).
- **Upstream status:** The upstream explicit-backend-route work in `ed11601539`, `572ecbe43d`, `5d7fea742c` and `0214eb4fdc` does not fix the fork's inherited-mode path. The per-task route `service_tier` in `tools/delegate_tool_tuning.py` covers explicit tiers only, so inheritance reuses its `without_service_tier` stripping and the same `ultrafast_offered` route/account gate.
- **Focused regression:** `scripts/run_tests.sh tests/agent/test_service_tier_logging.py tests/tools/test_delegate_ultrafast.py`.
- **Retirement:** Remove when upstream covers inherited Ultrafast and equivalent per-response requested/served auditing, without silent paid fallback or losing explicit-override precedence.
- **Rollback:** Revert the commit carrying `Fork-Patch: speed-tier-audit`.

## Active patch record: resumable delegation retention

- **Patch identity:** `resumable-delegation-retention`.
- **Behavior:** Keep resume-eligible (`unknown`/`interrupted`/`stalled`, unspent resume claim, parent or origin session) delegations in the durable ledger through terminal-cap and pending-cap pruning while they remain inside the seven-day retention window. Terminal-cap pruning deletes delivered rows first and pending completions last, so a pending child result is dropped only when nothing else is left to remove. A boot notice re-checks eligibility just before injection, which narrows but does not close the race; `claim_resume` remains the authority and refuses a notice that slips through. Retained resumable rows are bounded by age, not by the caps.
- **Source surfaces:** `tools/async_delegation.py`, `tools/delegation_resume.py`, `gateway/run_notifications.py`, and focused async-delegation recovery tests.
- **Upstream status:** NousResearch #128623 and PR #128634 retain delivered audit records, but do not preserve resume-eligible interrupted rows or guard the queued boot-notice race; no equivalent upstream fix found.
- **Focused regression:** `scripts/run_tests.sh tests/tools/test_delegation_resume.py tests/gateway/test_delegation_auto_resume.py`.
- **Retirement:** Remove this patch when upstream preserves resumable delegation rows through pruning and suppresses stale auto-resume notices at injection time.
- **Rollback:** Revert the resumable-delegation-retention fix commit.

## Active patch record: edge silence markers (S1)

- **Patch identity:** `trailing-silence-marker`.
- **Behavior:** Strip runs of standalone `NO_REPLY`/`[SILENT]`/`no reply` marker lines that open or end a substantive interactive reply before delivery and assistant-transcript persistence, so a model that emits the marker *and* prose delivers only the prose. The decision is made on the visible text after inline think blocks are removed, so reasoning plus a bare marker stays intended silence. A response of only marker lines collapses to one marker and still goes through the silence guard. Streaming holds previews while a segment opens with a marker line or its last line could become one, and strips a segment's edge markers before its tool-boundary edit. Preserve bare-marker suppression, fenced-code content, mid-sentence text, and autonomous-lane semantics.
- **Source surfaces:** `gateway/response_filters.py`, `agent/turn_final_response.py`, `gateway/run_turn.py`, `gateway/stream_consumer.py`, and gateway response-filter/stream/final-persistence regression tests.
- **Upstream status:** NousResearch #126581, #126646, and #126827 address bare-marker leaks on failed turns and segment-break previews; none strips a marker line from substantive replies.
- **Focused regression:** `HERMES_HEAVY_SLOT=off HERMES_HOME=<isolated-home> /Users/brianle/Repos/hermes-agent/.venv/bin/python -m pytest -q tests/gateway/test_trailing_silence_marker.py tests/gateway/test_response_filters.py tests/gateway/test_stream_consumer_silence.py`.
- **Retirement:** Remove this patch when released upstream behavior strips leading and trailing standalone markers at both persistence and every interactive streaming delivery boundary.
- **Rollback:** Revert the trailing-silence-marker fix commits.

## Active patch record: gateway human-turn silence re-prompt

- **Patch identity:** `gateway-silence-reprompt`.
- **Behavior:** Before a gateway turn closes, an exact `NO_REPLY`/`[SILENT]` final visible response on a user-typed turn receives one bounded gateway recovery execution for a short visible status. Only the retry guidance is ephemeral (API-only, applied to the cached agent for the recovery execution and restored afterwards); the first execution's messages, including its marker, continue as history. Internal notification turns, `reply_expected=False` turns, non-gateway lanes, and a second marker are not re-prompted. A recovery redirect preserves the newer human inbound ledger identity without changing the original silence contract.
- **Source surfaces:** `gateway/run_turn.py`, `gateway/run_turn_runner.py`, `gateway/turn_context.py`, `gateway/stream_consumer.py`, `gateway/stream_consumer_fallback.py`, and gateway silence regression tests.
- **Upstream status:** related merged PR [NousResearch/hermes-agent#111624](https://github.com/NousResearch/hermes-agent/pull/111624) adds the downstream fallback for a human silence marker; no upstream gateway re-prompt equivalent found.
- **Focused regression:** `scripts/run_tests.sh tests/gateway/test_folded_steer_silence_reprompt.py tests/gateway/test_silence_retry_lifecycle.py tests/gateway/test_stream_consumer_wecom_native.py`.
- **Retirement:** Remove when released upstream re-prompts a user-typed gateway silence marker once before applying its fallback.
- **Rollback:** Revert commits carrying `Fork-Patch: gateway-silence-reprompt`.

## Active patch record: goal continuation silence

- **Patch identity:** `goal-continuation-silence`.
- **Behavior:** Gateway-built standing-goal continuations (the post-turn continuation and the lifted-barrier wake) carry `reply_expected=False`, so a no-change tick that answers a bare `NO_REPLY` stays silent instead of posting the "No reply was written" fallback. A typed message absorbed into the same turn restores `reply_expected` through `MessageEvent.absorb_reply_expected`, so the human-turn guard is unchanged. Heartbeat and `/loop` wakeups follow the internal-notification-silence contract: their prompts ask for a bare `[SILENT]` on a no-change tick.
- **Source surfaces:** `gateway/run_goals.py` (`_synthetic_prompt_event`, `_post_turn_goal_continuation`, lifted-barrier wake) and `tests/gateway/test_goal_continuation_silence.py`.
- **Upstream status:** no upstream equivalent; upstream gateway goal continuations are plain synthetic text events.
- **Focused regression:** `scripts/run_tests.sh tests/gateway/test_goal_continuation_silence.py tests/gateway/test_goal_continuation_drain.py`.
- **Retirement:** Remove when upstream marks synthetic goal continuations as non-human for silence decisions.
- **Rollback:** Revert the goal-continuation-silence fix commit.

## Active patch record: turn session id binding

- **Patch identity:** `mcp-caller-cached-agent`.
- **Behavior:** `GatewayRunner._set_session_env` binds the turn's own `session_id` with the other per-turn session vars. Before, only agent construction published it, so a turn served by a cached agent ran with `HERMES_SESSION_ID` bound to `""`: MCP servers opted into `caller_identity` (relay) got no `_meta["hermes/caller"]` and refused with `caller_identity_required`.
- **Source surfaces:** `gateway/run.py` (`_set_session_env`) and `tests/gateway/test_session_env_session_id.py`.
- **Upstream status:** no upstream equivalent; upstream `_set_session_env` omits `session_id`.
- **Focused regression:** `scripts/run_tests.sh tests/gateway/test_session_env_session_id.py tests/tools/test_mcp_caller_identity.py`.
- **Retirement:** Remove when upstream binds the session id in the gateway turn's session context.
- **Rollback:** Revert the mcp-caller-cached-agent fix commit.

## Active patch record: Responses reasoning summary is never a reply

- **Patch identity:** `responses-reasoning-summary-not-reply`.
- **Behavior:** A Responses/Codex turn that stops cleanly with empty visible output never promotes its reasoning summary (the bolded title from `codex_reasoning_items` summary parts) to `final_response` or `api_content`. With `reply_expected=False` (relay "No reply needed", goal wakes) the turn resolves to `[SILENT]` and the gateway suppresses delivery; otherwise it walks the normal empty-response ladder (prefill, retry, fallback, generic empty copy). The exhausted-ladder preview and the content-filter refusal text no longer read the summary either. Chat-completions inline reasoning promotion (vLLM Nemotron parser, #109205) is unchanged.
- **Discriminator:** `agent.api_mode == "codex_responses"` or a non-empty `codex_reasoning_items` carrier on the normalized message; both mean the reasoning came from Responses summary items, not an inline parser field.
- **Source surfaces:** `agent/reasoning_summaries.py` (`is_responses_reasoning_summary`, the one detection rule), `agent/turn_final_response.py` (clean-stop promotion gate, and silence only for a contentless stop on a non-reply turn), `agent/turn_context.py` (`_turn_reply_expected` from `persist_user_display_metadata`), `gateway/run_busy.py` (`_fold_into_running_turn` refreshes `_turn_reply_expected` when a human message joins the turn), `agent/turn_empty_response.py` (`_terminal_empty` preview), `agent/turn_truncation.py` (`handle_content_policy_refusal`), and `tests/agent/test_responses_reasoning_summary_delivery.py`.
- **Evidence:** state.db rows 6518674 and 6519049 (session `20261008_135347_0099257a`, gpt-6.1-sol via a Codex proxy): content empty, `finish_reason=stop`, `api_content` equal to the 36-char summary title; gateway logged `response=36 chars` then `[Telegram] Sending response (36 chars)`.
- **Upstream status:** #133729 (route gate for chat-completions promotion) and #132997 (Anthropic summarized thinking) are merged; neither covers Responses summaries. No open issue or PR found for this path; contribute upstream.
- **Focused regression:** `scripts/run_tests.sh tests/agent/test_responses_reasoning_summary_delivery.py tests/agent/test_reasoning_only_stop_persistence.py tests/agent/test_empty_terminal_reasoning_surface.py`.
- **Retirement:** Remove when upstream releases an equivalent Responses-summary gate.
- **Rollback:** Revert the responses-reasoning-summary-not-reply commit.

## Active patch record: shared primary cooldown

- **Patch identity:** `shared-primary-cooldown`.
- **Behavior:** A primary route that is rate-limited, billing-limited, upstream-rate-limited, or overloaded (`FailoverReason.overloaded`, chiefly HTTP 529 and 503) writes an atomic, file-locked cooldown under `$HERMES_HOME/state/model_cooldowns.json`. Fresh agents adopt the configured fallback without calling the primary while the shared record is active. One process claims the outage notice; one successful primary response clears the record and emits the recovery notice. Generic 5xx and timeouts do not arm. Records with a non-finite `reset_at` or `recorded_at` are malformed and pruned.
- **Source surfaces:** `agent/shared_primary_cooldown.py`, `agent/fallback_cooldown.py`, `agent/agent_runtime_helpers.py`, `agent/chat_completion_helpers.py`, `agent/chat_completion_nonstream.py`, and `hermes_cli/fallback_cmd.py`.
- **Focused regression:** `scripts/run_tests.sh tests/agent/test_shared_primary_cooldown.py tests/agent/test_provider_fallback.py tests/agent/test_fallback_exhaustion_cooldown.py tests/hermes_cli/test_fallback_cmd.py` plus `evals/provider_fallback/probe_shared_primary_cooldown.py` (three `PROBE_OK` lines).
- **Retirement:** Remove when upstream provides equivalent shared cooldown, fresh-agent adoption, notice ownership, recovery clearing, and CLI status/clear controls.
- **Rollback:** Revert the commits carrying `Fork-Patch: shared-primary-cooldown`.

## Active patch record: process watcher context isolation

- **Patch identity:** `process-watcher-context-isolation`.
- **Behavior:** `arm_process_watcher` schedules `_run_process_watcher` on the gateway loop in a fresh `contextvars.Context()`. It runs on the tool thread that started the process, which can be a `delegate_task` child's. Scheduling copied that thread's context, so a process the child handed to its parent ran its watcher, and the completion turn it injects into the parent, with the child's delegated-child marker and session id. The parent's turns then read as a subagent's: `review_candidate` refused with `parent_only`, and its background processes were labelled subagent-owned. Startup-recovered watchers already run in the root context and resolve their profile scope through `_completion_event_scope`.
- **Source surfaces:** `gateway/run_notifications.py` (`arm_process_watcher`) and `tests/gateway/test_background_process_notifications.py`.
- **Upstream status:** upstream `main` has the same `arm_process_watcher` (from #112287) without the isolation, and no upstream fix exists. Contribute upstream.
- **Focused regression:** `scripts/run_tests.sh tests/gateway/test_background_process_notifications.py tests/tools/test_terminal_watcher_arming.py`.
- **Retirement:** Remove when upstream arms live watchers outside the caller's context.
- **Rollback:** Revert the commits carrying `Fork-Patch: process-watcher-context-isolation`.

## Active patch record: shutdown spool turn contract

- **Patch identity:** `shutdown-spool-turn-contract`.
- **Behavior:** `gateway/shutdown_flush.py` serialises `internal`, `allow_gateway_control`, `reply_expected` (when known), and JSON-safe `metadata` for every spooled `MessageEvent`, not only drain-deferred arrivals. Startup replay (`gateway/run_pending_recovery.py`) restores `reply_expected`, and the transcript-append fallback stamps `display_kind=internal_notification` and `display_metadata.reply_expected`. Previously a process-completion notice queued at shutdown came back as a human turn, so the agent's correct `NO_REPLY` drew "No reply was written for this message" after each restart. Old spool files without these keys recover as before.
- **Source surfaces:** `gateway/shutdown_flush.py` (`_serialise_value`, `_recover_one_payload`), `gateway/run_pending_recovery.py` (`_defer_followup`), and `tests/gateway/test_resume_queued_followup.py`.
- **Upstream status:** upstream `_serialise_value` keeps only text and routing fields; open upstream PR NousResearch/hermes-agent#95217 serialises internal routing for a different replay design and does not cover `reply_expected`. Contribute upstream.
- **Focused regression:** `scripts/run_tests.sh tests/gateway/test_resume_queued_followup.py -k machinery_silence`.
- **Retirement:** Remove when upstream spools and restores the internal flag and `reply_expected` for every queued event.
- **Rollback:** Revert the commits carrying `Fork-Patch: shutdown-spool-turn-contract`.

## Active patch record: proactive tool-result prune default

- **Patch identity:** `proactive-tool-result-prune-default`.
- **Behavior:** Enable the existing deterministic no-LLM tool-result prune by default at `compression.proactive_prune_tokens: 48000`; retain the 8000-character eligibility floor, 4096-token minimum reclaim gate, and recent-tail protection. `0` remains the explicit opt-out.
- **Source surfaces:** `hermes_cli/config_defaults.py`, `agent/agent_init.py` configuration attachment (unchanged), `website/docs/user-guide/configuration.md`, and proactive-prune regression tests.
- **Upstream status:** The pruning implementation is already present upstream, but upstream retains a zero default; no equivalent nonzero default was found during preflight.
- **Focused regression:** `python -m pytest -q tests/agent/test_proactive_prune_config.py tests/agent/test_proactive_tool_result_pruning.py tests/agent/test_proactive_prune_rearm_threshold.py tests/agent/test_proactive_prune_loop_wiring.py`.
- **Retirement:** Remove the fork-only default, documentation, and default-specific regression when released upstream enables an equivalent nonzero default with the same tail protection and reclaim gating.
- **Rollback:** Revert the commit carrying `Fork-Patch: proactive-tool-result-prune-default`.

## Active patch record: state.db compaction at next gateway start

- **Patch identity:** `state-db-compact-at-start`.
- **Behavior:** `hermes sessions optimize --at-next-start` records a one-shot request in `state.db.compact-at-start.json` beside the store without opening it, so it works while the gateway runs (`--cancel-next-start` withdraws it). `start_gateway` honors it right after the PID-file claim, before the control socket, adapters, cron, the housekeeping worker or any SessionDB handle exist, running the same `SessionDB.vacuum()` as `hermes sessions optimize` (FTS merge, VACUUM, TRUNCATE checkpoint) and logging before/after size at INFO. It keeps the request and logs a WARNING instead when a foreign process holds the store, a release promotion awaits this gateway's acknowledgement (`release-txn.json`), or free disk is below about twice the live data plus 1 GiB. The rewrite renews the startup-watchdog lease (and systemd `EXTEND_TIMEOUT_USEC`) every 60s only while SQLite's VM or the store/WAL files show progress. Attempts are counted before the rewrite and the request is dropped after three, so a start killed mid-VACUUM cannot loop. It never raises into startup.
- **Why core:** the held-store refusal (#110054) correctly blocks a live rewrite, and automatic VACUUM needs a fresh prune plus a quiet store, so a gateway-owned install had no supported way to reclaim space after a bulk delete. Only the gateway's own startup can run before its own handles exist.
- **Source surfaces:** `hermes_state_compaction.py`, `gateway/run.py` (`_run_requested_state_db_compaction` and its call in `start_gateway`), `hermes_cli/sessions_cmd.py`, `hermes_cli/subcommands/sessions.py`, `website/docs/user-guide/sessions.md`, and `tests/hermes_state/test_compact_at_next_start.py`.
- **Upstream status:** no upstream issue or PR proposes deferred compaction. Related: #84525 (optimize under live holders, fixed by the refusal), #121324/#121783 (prune exempted from the held-store guard), #57752 and #112105 (auto-VACUUM gating). Candidate for an upstream contribution.
- **Focused regression:** `scripts/run_tests.sh tests/hermes_state/test_compact_at_next_start.py tests/hermes_cli/test_sessions_held_store_gate.py tests/hermes_state/test_auto_vacuum_holder_gate.py tests/hermes_state/test_startup_maintenance_lease.py`.
- **Retirement:** Remove when upstream provides a supported way to compact a gateway-held store, either at startup or by an equivalent quiesced path.
- **Rollback:** Revert the commits carrying `Fork-Patch: state-db-compact-at-start`. A leftover `state.db.compact-at-start.json` is then inert and can be deleted.

## Active patch record: shutdown spool fidelity

- **Patch identity:** `shutdown-spool-fidelity`.
- **Behavior:** a queued caption-less attachment (image, voice, file) is spooled and recovered: flush and recovery both reject only slots with neither text nor `media_urls`/`media`, and the transcript-append fallback writes the gateway's media placeholder when text is empty. Every spooled `MessageEvent` also keeps `message_type`, `media_text_inlined`, `reply_to_text`, `reply_to_author_id`, `reply_to_author_name`, and `reply_to_is_own_message`, and startup replay restores them, so a queued reply keeps its quoted context and voice/audio/document routing and text-inlining replay as they arrived. Old spool files without these keys recover as before (TEXT, no reply context).
- **Source surfaces:** `gateway/shutdown_flush.py` (`has_user_content`, `_serialise_value`, `_recover_one_payload`), `gateway/run_pending_recovery.py` (`_defer_followup`), and `tests/gateway/test_shutdown_spool_fidelity.py`.
- **Upstream status:** upstream v0.21.6 (`818c13be`) writes media-only events but its recovery still rejects empty text, so they stay spooled forever; it also drops reply context, `media_text_inlined`, and `message_type`. Contribute upstream.
- **Focused regression:** `scripts/run_tests.sh tests/gateway/test_shutdown_spool_fidelity.py`.
- **Retirement:** Remove when upstream spools and replays media-only events and reply context.
- **Rollback:** Revert the commits carrying `Fork-Patch: shutdown-spool-fidelity`.

## Active patch record: release-owned config writes

- **Patch identity:** `release-owned-config-writes`.
- **Behavior:** Refuse config.yaml writes from a Hermes build whose config schema is newer than the live immutable release's, including first-run seeding before any parent directory or template copy, so a dev worktree or sync candidate cannot stamp `_config_version` past what `hermes update` accepts. The live release, older or equal-schema builds (such as a previously-live release still running after promotion), and the process-local updater context write normally. Agent-authored edits through the file tool are out of scope: they never stamp a schema. The refusal names the config path, running root and schema, live release and schema, and the live release directory whose hermes to run.
- **Source surfaces:** `hermes_cli/release_config_owner.py`, `hermes_cli/config.py` `seed_config_file` (before parent creation/template copy) and `_write_config_state`, `utils.py` `_atomic_write` (the primitive under every atomic config.yaml writer), `hermes_cli/main.py` updater boundary, and `tests/hermes_cli/test_release_owned_config_writes.py`.
- **Upstream status:** Upstream `main` has no equivalent guard. The stamp that blocked the 2026-10-09 update came from the v0.21.6 sync candidate's schema 50, written into a home on release schema 49.
- **Focused regression:** `scripts/run_tests.sh tests/hermes_cli/test_release_owned_config_writes.py`.
- **Retirement:** Remove this patch when upstream prevents foreign Hermes builds from writing release-managed config or makes config schema ownership independent of the running release.
- **Rollback:** Revert the commits carrying `Fork-Patch: release-owned-config-writes`.

## Active patch record: Telegram first-poll health

- **Patch identity:** `telegram-first-poll-health`.
- **Behavior:** While the current polling generation has not proven progress, its getUpdates requests use Telegram `timeout=0`, and the read timeout drops by the matching allowance. A new gateway or reconnect then logs `Telegram polling confirmed healthy` within about 1s instead of after a 10s idle long poll. Health still requires a real successful current-generation getUpdates response. Stale or untagged generations and every post-progress poll keep PTB's long poll.
- **Source surfaces:** `plugins/platforms/telegram/adapter.py` (`_non_blocking_get_updates_kwargs`, `_polling_health_unproven`, `_instrument_polling_request`), `tests/plugins/test_telegram_first_poll_fast_ptb.py`, and [Telegram first-poll health](maintenance/telegram-first-poll-health.md).
- **Upstream status:** Upstream has no polling-generation health gate. PTB 22.8 `Updater.start_polling` uses one timeout for every poll. No equivalent upstream fix found.
- **Focused regression:** `scripts/run_tests.sh tests/plugins/test_telegram_first_poll_fast_ptb.py tests/plugins/test_telegram_polling_progress_ptb.py tests/gateway/test_telegram_polling_progress.py`.
- **Retirement:** Remove when upstream or PTB proves first-poll health without an idle long poll. Review on any PTB 23+ upgrade (private `RequestParameter`).
- **Rollback:** Revert the commit carrying `Fork-Patch: telegram-first-poll-health`.

## Active patch record: update post-swap claim

- **Patch identity:** `update-post-swap-claim`.
- **Behavior:** The immutable post-swap child claims the update receipt and the Windows resume token by unlinking its hand-off file before it resumes them. After the child exits, `_hand_off_post_swap` (`hermes_cli/update_cmd.py`) asks `reclaim_handoff` (`hermes_cli/immutable_update_handoff.py`) whether the hand-off is still on disk. If it is, whatever the exit code, the parent restores the receipt, records `post_swap_handoff` as failed, writes the incomplete marker and the gateway exit code, and keeps the resume token. Previously any integer exit code moved ownership to the child, so a staged interpreter that crashed while starting (exit 1) lost the receipt and recorded no failure. A claiming child's receipt and exit code stay authoritative. The payload format is unchanged.
- **Source surfaces:** `hermes_cli/update_cmd.py` (`_hand_off_post_swap`, `_run_post_swap_phase`), `hermes_cli/immutable_update_handoff.py` (`reclaim_handoff`), `hermes_cli/update_handoff.py` (`handoff_path`), and `tests/hermes_cli/test_update_post_swap_claim.py`.
- **Upstream status:** Fork-only. The immutable release hand-off does not exist upstream.
- **Focused regression:** `scripts/run_tests.sh tests/hermes_cli/test_update_post_swap_claim.py`.
- **Retirement:** Remove with the immutable post-swap hand-off.
- **Rollback:** Revert the commits carrying `Fork-Patch: update-post-swap-claim`.

## Active patch record: dead index-lock scan retry

- **Patch identity:** `dead-index-lock-scan-retry`.
- **Behavior:** On non-Windows launch recovery, keep polling a held-open process scan that is temporarily unknowable until the existing five-second deadline; if it remains unknowable, leave the lock in place fail-closed. Windows behavior is unchanged.
- **Source surfaces:** `hermes_cli/_early_recovery.py` and `tests/hermes_cli/test_update_interrupted_pull.py`.
- **Focused regression:** `scripts/run_tests.sh tests/hermes_cli/test_update_interrupted_pull.py`.
- **Retirement:** Remove when the upstream recovery path retries transiently unknown holder scans without stranding a dead `index.lock`.
- **Rollback:** Revert the commit carrying `Fork-Patch: dead-index-lock-scan-retry`.

## Active patch record: updater PID incarnation

- **Patch identity:** `updater-pid-incarnation`.
- **Behavior:** An updater's own PID is its current incarnation only when the recorded creation time is within the strict 5 ms own-process epsilon. A high-resolution `ct:N.000` from an earlier incarnation no longer passes because the process started at `N.5`. macOS shell hand-offs stay compatible because `marker.sh` reads the kernel's sub-second creation time instead of writing whole seconds, and the no-site Python reader uses `proc_pidinfo`. Cross-process tolerance and no-ct/unreadable-clock behavior are unchanged.
- **Source surfaces:** `hermes_cli/update_lock.py` (`_incarnation`, `_stdlib_create_time`, `_darwin_create_time`), `scripts/desktop-update/marker.sh` (`proc_ct`), `tests/hermes_cli/test_update_incarnation.py`, and the native macOS tests in `tests/scripts/desktop_update/`.
- **Upstream status:** The shell clock is salvaged from upstream PR #134551 with original authorship. Upstream PR #135407 provided the stdlib reader approach but keeps the `a28a5d03` / #134928 whole-second exception, which this patch removes.
- **Focused regression:** `scripts/run_tests.sh --file-retries 0 -j 2 tests/hermes_cli/test_update_incarnation.py tests/hermes_cli/test_update_pause_record.py tests/hermes_cli/test_update_lock.py tests/scripts/desktop_update/test_macos_handoff_identity.py tests/scripts/desktop_update/test_desktop_update_posix_marker_macos_ct.py`.
- **Verification:** The new regression files fail on the frozen base (5 failed, 8 passed) and pass on the candidate (13 passed). The final eight-file run, including all four wider-regression failures, passes on native macOS (172 passed, 12 platform skips), using the private test environment and CI-style isolated Git configuration. The two positive handoff fixtures use a real parent instead of a child stand-in; the runtime-host ancestry guard is unchanged. Source-only fork validation reports 0 problems.
- **Retirement:** Remove once upstream ships a precise macOS shell producer and strict own-PID validation, and these regressions pass there without the fork patch.
- **Rollback:** Revert the commits carrying `Fork-Patch: updater-pid-incarnation`. Revert all of them together: restoring the strict rule without the precise producer would break macOS hand-offs.

## Additional maintenance units

The following units are part of the enrolled maintenance surface. Load the row's
contract before acting on its trigger; a full maintenance run accounts for every
row:

| Unit | Required behavior | Load when | Contract |
|---|---|---|---|
| CI slow-file timeouts | Keep known-slow hosted test files within measured per-file bounds without changing the default for other files | Hosted shard timeout evidence, `FILE_TIMEOUTS`, or slow-file selection changes; also load Portable CI | [CI slow-file timeouts](maintenance/ci-slow-file-timeouts.md) |
| Fallback mid-turn restore | Restore an eligible primary or earlier fallback between model requests while preserving cooldown and route invariants | Provider fallback recovery, mid-turn retry, or fallback stay-limit changes | [Fallback mid-turn restore](maintenance/fallback-mid-turn-restore.md) |
| Gate flake quarantine | Keep the PR gate blocking while isolating only reproduced host/process flakes with nightly evidence | Gate quarantine lists, exact-SHA flake evidence, or shard disposition changes; also load Portable CI and Repository CI contract | [Gate flake quarantine](maintenance/gate-flake-quarantine.md) |
| Goal continuation quiet | Deduplicate unchanged continuation notices across waits and preserve meaningful goal outcomes | Goal wait, continuation notice, automatic silence, or lifecycle deduplication changes | [Goal continuation quiet](maintenance/goal-continuation-quiet.md) |
| Install and scratch GC | Reclaim orphaned installs and scratch data only under ownership, age, lock, lease, and deletion-window safeguards | PM install cleanup, scratch pruning, housekeeping, worktree pruning, or deletion-window changes; also load runtime ownership | [Install and scratch GC](maintenance/install-and-scratch-gc.md) |
| One-shot task governing skill loading | Load task-governing procedural skills in one-shot sessions without restoring omitted maintenance tooling | One-shot footprint, prompt skill admission, or governing-skill loading changes | [One-shot task governing skill loading](maintenance/oneshot-task-governing-skill-loading.md) |
| Portable CI | Keep local and hosted source gates reproducible, isolated, and aligned across supported lanes | `bin/ci`, `scripts/ci/portable.py`, toolchain pins, workflow lanes, or source-gate isolation changes | [Portable CI](maintenance/portable-ci.md) |
| Repository CI contract | Preserve exact-SHA admission, gate aggregation, workflow dispositions, and the boundary between local evidence and qualification | Repository CI commands, workflow graph, required statuses, or gate/nightly policy changes | [Repository CI contract](maintenance/repository-ci-contract.md) |
| Shared primary cooldown | Share provider outage cooldowns across agents while preventing fallback replies from clearing them | Fallback classification, cooldown state, outage/recovery notices, or cross-process route adoption changes | [Shared primary cooldown](maintenance/shared-primary-cooldown.md) |
| Updater external supervisor sweep | Protect newly spawned supervised gateways while draining pre-existing processes for supervisor hand-back | Update restart sweeps, gateway PID snapshots, launchd or external-supervisor ownership changes; also load runtime ownership | [Updater external supervisor sweep](maintenance/updater-external-supervisor-sweep.md) |
| Internal recovered follow-up retirement | Preserve, decline, visibly note, and quarantine shutdown follow-ups when identity is missing or uncertain; retire durable recovered inputs without replay | Shutdown follow-up spooling, restart recovery admission/retirement, or durable recovered-turn failure handling | [Internal recovered follow-up retirement](maintenance/internal-followup-retirement.md) |

## Active patch record: internal recovered follow-up retirement

Fork patch identity: `internal-followup-retirement`.

- **Load when:** changing shutdown follow-up spooling, restart recovery admission/retirement, or the associated durable input and visible-note paths.
- **Behavior:** Id-less recovered follow-ups carry a synthetic recovery id outside transport identity; the id is persisted on the user row and retires the spool once that row is durable. Legacy spools without a recovery id use exact content plus spool timestamp as an uncertain fallback. Every durable input—recovery id, platform message id, or legacy content+timestamp match—is always declined with the hedged possible-duplicate note, quarantined after the note is delivered, and never silently deleted or replayed, regardless of resume-marker freshness or scheduler admission. If the note is unavailable, retain the spool within the 7-day bound. Non-durable inputs proven not to have been handled keep the assertive decline notice. Notices are translated inside the event's profile scope. Busy pending-slot successors propagate their terminal turn result back to recovery. The recovery outcome is recorded per spool, including `resumed` or `skipped:turn-failed`, without a predecessor outcome swallowing the follow-up. Stale note-unavailable spools are deleted after the 7-day `_STALE_FOLLOWUP_RETENTION_SECS` bound with `skipped:stale:note-unavailable`, without `.bad` quarantine.
- **Source surfaces:** `gateway/run_pending_recovery.py`, `gateway/run_restart_recovery.py`, `gateway/run_turn.py`, `locales/*.yaml` (`followup_declined_notice`, `followup_possible_duplicate_notice`), `gateway/platforms/base.py`, `gateway/session_transcript.py`, `hermes_state_messages.py`, `gateway/shutdown_flush.py`, `tests/gateway/test_shutdown_followup_restart.py`, and `tests/gateway/test_proxy_mode.py`.
- **Focused regression:** `uv run --offline --group dev pytest -q -p no:cacheprovider tests/gateway/test_restart_recovery_outcomes.py tests/gateway/test_restart_resume_admission_identity.py tests/gateway/test_restart_resume_pending.py tests/gateway/test_restart_resume_policy.py tests/gateway/test_restart_resume_real_adapter.py tests/gateway/test_shutdown_followup_restart.py`.
- **Rollback:** Revert the commit carrying `Fork-Patch: internal-followup-retirement`.

Fork-Patch: web-search-cost-controls.

- **Patch identity:** `web-search-cost-controls`.
- **Behavior:** Deduplicate successful paid web searches across concurrent Hermes processes with route-aware cache identity, keep rescued or failed responses out of the cache, and surface warn-only per-session request thresholds. Keyed and keyless ring routes, opt-in Tavily tiers, Parallel search modes, and direct and managed Perplexity routes remain isolated. Billed managed Firecrawl fallback attempts count toward the same session warning budget.
- **Source surfaces:** `tools/web_tools.py`, `tools/web_result_cache.py`, `tools/web_tools_rescue.py`, and focused web-tool tests.
- **Focused regression:** `scripts/run_tests.sh tests/tools/test_web_result_cache.py tests/tools/test_web_keyless_rescue.py tests/tools/test_web_providers.py`.
- **Retirement:** Remove when released upstream provides equivalent cross-process paid-search deduplication, route-aware cache identity, and visible warning behavior.
- **Rollback:** Revert the commit carrying `Fork-Patch: web-search-cost-controls`.

## Update

Each maintenance unit owns its patches' provenance, proof surface, and retirement
condition; there is no central ledger. Every non-merge commit after the trailer floor recorded in
`scripts/check_fork_patches.py` carries one `Fork-Patch: <identity>; ...` trailer per
identity. A unit owns an identity by naming it as a backticked token on an identity line
(a line, or its indented continuation, that says "identity" or "identities" before the
first backtick); other code spans do not own. Commits whose identity is `evidence` are
records, not patches. This contract's own patch identity: `maintenance-contract`. Upstream release ancestry is excluded from fork trailer classification. Historical
rebases can rewrite the floor SHA; the checker requires the floor to be an ancestor of HEAD,
locates a rewritten default floor by its exact subject, and fails clearly if that is gone.
A maintenance unit may repair missing published metadata with an explicit
`Fork-Patch-Backfill: <stable-patch-id>; <owned-identity>` line. This covers only the
reviewed patch content without rewriting shared history or advancing the floor.
Fork-Patch-Backfill: dd8b51a7f52d926fc084cd7e84bc8b72a651ff0f; evidence
Published upstream maintenance merge `c8d3ae60f4f6` arrived without a trailer.
New commits still require trailers, including the final squash merge message.
A trailer proves classification, not functional coverage. Retire a patch only after its
regression passes on the selected upstream release without the local implementation.
Load [runtime ownership](maintenance/runtime-ownership.md) whenever changing
installation, update/rollback tooling, scheduled procedures, or recovery evidence.
These are support files for this contract, not independently scheduled targets.

Fetch `origin/main` and upstream release tags, select the newest upstream release,
and reconcile each logical patch against it in an isolated worktree. Compare
upstream `main` separately for unreleased fixes worth explicit temporary backports.
Use `scripts/sync_fork_candidate.py` only as a candidate builder: a successful
release merge or published candidate does not authorize promotion. Refresh release-tag
selection before reporting current and prove the selected tag is an ancestor of
the proposed fork head. Report upstream-main divergence separately from release
currency. Preserve this release policy when applying generic maintenance guidance.
For the Background exception, freeze its exact upstream cutoff and use it as the
source checker baseline while separately proving latest-release ancestry. Resume
that candidate across interruptions rather than moving the cutoff. Return to
release selection after adoption without discarding the exception ancestry.

## Verify

- Run `scripts/run_tests.sh` for every affected patch's proof surface in its
  maintenance unit, including plugin discovery when extension code changes.
- If media or browser fixtures fail on this host, check for synthetic `198.18.0.0/15`
  DNS answers before attributing a regression. Preserve the SSRF guard; verify
  the environment cause instead of blanket-skipping failures.
- Run `scripts/check_fork_patches.py` against the intended source/profile pair.
  Its trailer, unit-ownership, and registration checks do not establish runtime
  acceptance; verify native update receipts and running identities as well.
- Prove selected-tag ancestry, review the remaining fork diff, and read back the
  published `origin/main` SHA. Record a failed stage instead of reporting current.
- For separately authorized activation, use the runtime support file's host-specific
  acceptance and recovery requirements. Preserve unresolved acceptance gaps until
  demonstrated behavior closes them.

Release adoption preserves published fork history with a merge commit. The maintained
branch must allow merges while retaining its required App-bound local CI, strict checks,
admin enforcement, and prohibition on force pushes. Conflict resolution and review happen
on a candidate branch before normal protected landing. Unattended sync disables rerere
so unreviewed remembered resolutions cannot silently resolve a new release conflict.

## Active patch record: routine restart resume note

- **Patch identity:** `routine-restart-resume-note`.
- **Behavior:** Present pending-turn recovery as a neutral continuation instruction, not as news about a restart, shutdown, interruption, or the gateway's availability. Preserve command/tool replay safety and reconcile-before-retry guidance for ambiguous non-idempotent effects. A new user message remains first priority and stale unfinished work is skipped unless that message asks for it; `continue` completes the pending task from its first unrecorded step without acknowledgement, while `ask` runs no tools and asks in one plain line whether to carry on with the named pending step. Redelivered replies retain only a neutral duplicate hint; flood-control markers keep their partial-delivery warning.
- **Source surfaces:** `gateway/run.py`, `gateway/delivery_ledger.py`, `gateway/run_startup.py` marker consumption, `plugins/platforms/telegram/chat_budget.py`, `tools/process_registry_notifications.py` (neutral lifecycle-caused process/delegation notices), `tools/async_delegation.py` and `tools/delegation_resume.py` (outcome-only recovery text), `hermes_cli/goals.py` (neutral lifted-barrier text), `hermes_cli/cli_shutdown.py` (neutral delegation stop reason), `cron/scheduler.py` and `gateway/run_shutdown.py` (outcome-only cron interruption state and notices), `locales/en.yaml` (neutral cron notice with English fallback for untranslated locales), `tui_gateway/session_auto_continue.py` (TUI crash auto-continue note), `gateway/config.py` and `maintenance/restart-continuation.md` policy wording, and resume, delivery-ledger, synthetic-prompt, queued-follow-up, and Telegram trigger tests.
- **Upstream status:** Upstream issue search found related restart-resume work including #57056, #111644, #117711, #120963, and #127919; no exact upstream equivalent for neutral model-visible wording and transport-neutral duplicate markers was identified. Related open PRs #117711 and #118031 explicitly retain restart-cause wording, so this fork patch intentionally diverges pending a broader upstream design decision.
- **Focused regression:** `scripts/run_tests.sh tests/gateway/test_restart_resume_policy.py tests/gateway/test_restart_resume_pending.py tests/gateway/test_resume_queued_followup.py tests/gateway/test_delivery_ledger.py tests/gateway/test_delivery_ledger_process_home.py tests/gateway/test_telegram_internal_delivery_recovery.py tests/gateway/test_telegram_daily_call_counter.py tests/agent/test_synthetic_prompt.py tests/tui_gateway/test_auto_continue.py tests/e2e/core/delivery/test_messaging_exactly_once.py tests/tools/test_routine_stop_notifications.py tests/hermes_cli/test_cli_delegation_stop_notice.py tests/cron/test_shutdown_interrupt.py tests/gateway/test_cron_interrupt_notification.py tests/gateway/test_cron_active_work_drain.py tests/tools/test_delegate_interrupt_reason.py tests/tools/test_delegation_recovery_diagnostics.py tests/hermes_cli/test_goal_parked_idle_wake.py tests/gateway/test_goal_parked_idle_wake.py`.
- **Retirement:** Remove when released upstream provides equivalent neutral resume instructions, preserves all replay/reconciliation safety clauses, and uses a transport-neutral duplicate marker for ambiguous redelivery.
- **Rollback:** Revert the commit carrying `Fork-Patch: routine-restart-resume-note`.

## Active patch record: child-parent relay guard

- **Patch identity:** `child-parent-relay-guard`.
- **Behavior:** The Relay MCP tool refuses a delegated descendant's send to its immediate parent or to the session that started its in-process delegation chain (its topic), at any depth, before the call server is acquired. The chain root is not read from session rows, because child rows on TUI and desktop carry the surface's source instead of `subagent`, so stored lineage cannot tell a nested child from a topic. `_build_child_agent` stamps `_delegation_root_session_id` on each child, inherited from its parent agent, and both child-run entries pass it to `delegated_child_context`, so the root survives worker threads that do not inherit the delegating turn's context. For the `relay send` CLI, Hermes carries one `RELAY_PARENT_ADDRESS=hermes:<profile>/<chain root>` through delegated subprocess setup, including foreground/background terminal, execute_code and other `delegated_child_subprocess_env` spawns. An in-process child's own value replaces an inherited one, matching the terminal wrapper's export; outside an in-process child, inherited values pass through untouched. Because relay accepts one address, a grandchild's CLI guard covers the chain root rather than its immediate parent; MCP still refuses both. The shared `BaseEnvironment._wrap_command` exports it after sourcing the snapshot for local and remote backends; Managed Modal prefixes the same export on its separate exec path. Relay compares parsed addresses before reading the body or using transport (C27, 0xble/relay#45). This is a cooperative CLI guard: nested environment cleaning, unsetting or overriding the variable bypasses it, and arbitrary-shell enforcement is not provided. Both it and `HERMES_DELEGATED_PARENT_SESSION_ID` are excluded from shared shell snapshots to prevent later top-level commands inheriting a child's parent identity. Top-level sends and child sends to other sessions are unchanged.
- **Source surfaces:** `agent/relay_guard.py`, `agent/delegation_context.py`, `tools/code_execution_env.py`, `tools/environments/base_session_env.py`, `tools/environments/base.py`, `tools/environments/managed_modal.py`, `tools/mcp_tool_handlers.py`, `tools/delegate_tool_child_run.py`, and `tests/agent/test_relay_guard.py`.
- **Upstream status:** Fork-only guard carried by PR #541. Requires a relay release containing 0xble/relay#45; until then the CLI path is unguarded and the MCP path still holds.
- **Focused regression:** `scripts/run_tests.sh tests/agent/test_relay_guard.py tests/tools/test_mcp_caller_identity.py tests/tools/test_mcp_tool.py tests/tools/test_snapshot_session_id_leak.py -q`. Covers grandchildren and deeper descendants, unrelated peers, chain-root rather than older-ancestor resolution, surface-labelled child rows (TUI, desktop) at depths 1 to 3, a grandchild run on a fresh worker thread, operation without a session database, chain-root CLI stamping, and in-process precedence over inherited addresses.
- **Retirement:** Remove when upstream provides equivalent immediate-parent and lineage-topic coverage for delegated descendants through MCP, plus the cooperative CLI topic-address guard.
- **Rollback:** Revert the commits carrying `Fork-Patch: child-parent-relay-guard`.

## Active patch record: update main drop guard

- **Patch identity:** `update-main-drop-guard`.
- **Behavior:** Before a stable update moves the checkout, resolve the currently live release or current checkout head independently from the rescue capture path, then refuse a target that would remove commits already present on both the live release and `origin/main`. Record intentional overrides with `--allow-drop-main`; skip the check only for release channels whose own admission contract applies. Immutable updates also carry a live unmerged installed candidate into the target as a local-only merge commit without moving the checkout or index. Conflicts stop before staging or activation, naming the candidate and `--replace-candidate <exact SHA>` recovery. Explicit replacement drops only the named live candidate. Local `refs/hermes/carry-forward/` refs retain synthetic commits for future updates.
- **Source surfaces:** `hermes_cli/update_cmd.py`, `hermes_cli/update_main_guard.py`, `hermes_cli/update_cmd_candidate.py`, `hermes_cli/immutable_releases.py`, updater parser and custody, and update-related integration tests. Developer contract: `website/docs/developer-guide/cli-internals.md`.
- **Focused regression:** Run the clean-environment repository pytest command against `tests/hermes_cli/test_update_main_guard.py`, `tests/hermes_cli/test_update_live_candidate.py`, `tests/hermes_cli/test_update_autostash.py`, `tests/hermes_cli/test_update_fleet_restart_pending.py`, and `tests/hermes_cli/test_source_channel_integration.py`. Live-candidate coverage uses real Git DAGs, archive materialization, pointer activation and durable receipt readback. It covers conflict refusal, exact replacement and handoff flags, merged/contained candidates, repeated-update idempotence, and historical squash/cherry-pick landings followed by reverts, deletions or superseding edits. Landing requires graph ancestry or GitHub commit-associated PR evidence of a merge into the source repository's target branch or main. Carry-forward requires an exact open PR head and a complete listing with no relevant merged PR, plus the existing source-head check. No PR, offline/API errors, unsupported origins and incomplete evidence refuse with the live SHA and `--replace-candidate <SHA>`, never infer landing from patch IDs or tree content. Nearby-line context changes before squash landing and later reverts, deletions and edits return the bare published target.
- **Upstream check:** NousResearch/hermes-agent main `4dcef215cfe41bd2d02c2d1dad6e257a18101216` has update downgrade protection but no immutable-release carry-forward implementation. PR and issue searches for update candidates, unmerged updates, and carry-forward found no equivalent.
- **Retirement:** Remove when the maintained upstream update path preserves live `origin/main` commits and unmerged installed candidates with equivalent carry-forward, conflict, receipt, override, and orphan-rescue coverage.
- **Rollback:** Revert the commits carrying `Fork-Patch: update-main-drop-guard`.

## Eager plugin tools

Fork patch identity: `eager-plugin-tools`. Plugin-owned tools may opt into the direct schema surface while explicit user deferral remains authoritative. See [the maintenance unit](maintenance/eager-plugin-tools.md).

## Session controls

Fork patch identity: `session-controls`. One session can pause, resume, clear or replace another session's goal, and pause, resume or stop another session's loop, authorized by a fresh verbatim user quote or a Telegram Approve/Deny press. See [the maintenance unit](maintenance/session-controls.md).

## TUI Prompt Test Isolation

Fork patch identity: `test-isolation`. Fake-agent prompt-submit rewind tests share a containment fixture that stubs real build/turn/deferred execution and joins the submit dispatcher before their mocks and session mapping are removed. A prior deferred-run-only stub left an earlier dispatcher able to enter a real prompt after monkeypatch teardown and contaminate a later poller test. Preserve every rewind assertion, real-conversation rewind coverage and the poller's owner-session assertion. Production behavior is unchanged.

Verify the complete `tests/tui_gateway/test_tui_gateway_server.py` through `scripts/run_tests.sh` with file retries disabled. Retire this test-only adaptation when the accepted upstream tests contain their dispatchers across teardown and retain equivalent rewind and notification-ownership coverage.
