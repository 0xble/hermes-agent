# Hermes fork maintenance

## Background

Canonical source: `/Users/brianle/Repos/hermes-agent`, published as
[`0xble/hermes-agent`](https://github.com/0xble/hermes-agent), branch `main`.
Upstream is [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent),
default branch `main`, remote `upstream-live`. Accepted release baseline:
`v2026.9.24 + qualified checkpoint`, `eb8d21f482142c236550762ebdb5df4004c39696`.
Brian authorized a newer stable pre-tip checkpoint on 2026-09-30 for one
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
| Fork CI | Reproducible patch proof surfaces, hermetic Git fixtures, and complete-suite access on bounded runners | CI, test harness, Git fixture, or proof-surface changes | [Fork CI](maintenance/fork-ci.md) |
| CI toolchain pinning | Resolve local gate uv and Node binaries from the exact repository pins without changing hosted runner semantics | `scripts/ci/toolchain.json`, `scripts/ci/portable.py`, or local gate tool resolution changes | [CI toolchain pinning](maintenance/ci-toolchain-pin.md) |
| Launchd account home | Keep service labels and plist placement on the same real account identity even when process HOME changes | Service naming, sandbox gateway startup or launchd profile ownership | [Launchd account home](maintenance/launchd-account-home.md) |
| Build store dependencies | Resolve package hook dependencies from the caller-owned build store, with scoped cleanup and ordinary sealed-store fallback | PM install lifecycle, dependency lookup, archived web builds, or ARM64 Windows ripgrep staging | [Build store dependencies](maintenance/build-store-dependencies.md) |
| Documents extra | Opt-in, platform-gated document toolchain extra that bundles never preinstall and PM detects once installed | Optional extras, opt-in or platform gates, extra anchors, or document toolchain pins | [Documents extra](maintenance/documents-extra.md) |
| TTS discovery without installation | Defer optional SDK installation until TTS use instead of blocking unrelated turns | TTS capability registration, lazy SDK imports, or tool-schema discovery changes | [TTS discovery](maintenance/tts-discovery-no-install.md) |
| Goal lifecycle | Complete judge criteria, conversational recovery of blocker pauses, and durable command authority during judging | Goal judging, admission, continuation, or concurrent goal-command changes | [Goal lifecycle](maintenance/goal-lifecycle.md) |
| Internal notification silence | Internal process/delegation turns use an exact silence contract; parked-goal notices are durable and state-change deduplicated | Internal notification footer, process/delegation wake delivery, parked-goal status notices, or their regressions | [Internal notification silence](maintenance/internal-notification-silence.md) |
| Goal status flood retry | Important parked, continuing, wait-ended, achieved, paused, and blocked notices survive short Telegram flood windows without blocking the turn pipeline | Goal status notice delivery, flood-control classification, or shared cron/notice retry budgets | [Goal status flood retry](maintenance/goal-notice-flood-retry.md) |
| Parked goal idle wake | A parked goal resumes when its wait ends, even when no completion turn arrives (restart-killed process, no notify, elapsed timer), and stale restart-resume markers cannot wedge it forever | Goal wait barriers, the gateway loop wakeup watcher, TUI notification poller, or restart process cleanup changes | [Parked goal idle wake](maintenance/goal-parked-idle-wake.md) |
| Restart-parked goal wake | A stale `resume_pending` marker yields ownership to the idle goal ticker after restart auto-resume's freshness window, with CAS fencing against a refreshed marker | Restart-interrupted parked goals and gateway idle wake ownership | [Restart-parked goal wake](maintenance/goal-restart-parked-wake.md) |
| Gateway stop stays stopped | `/stop` pauses the standing goal and holds the completions it produced until the user's next turn | Gateway `/stop`, completion injection, or goal pause/revival changes | [Gateway stop stays stopped](maintenance/gateway-stop-stays-stopped.md) |
| Command replies keep paths as text | Slash-command replies never auto-upload local files they mention, such as a goal's handoff path | Slash dispatch, `EphemeralReply`/`CommandReply`, or bare-path delivery changes | [Command reply paths](maintenance/command-reply-bare-paths.md) |
| Telegram rendering | Preserve rich mode selection and prompt/delivery agreement | Telegram rendering changes and every upstream sync; also load runtime ownership before promotion | [Telegram rendering](maintenance/telegram-rendering.md) |
| Session-link rendering | Keep internal session references on Desktop only while preserving Telegram link-target safety | Session-search result or platform dispatch changes | [Session-link rendering](maintenance/session-link-rendering.md) |
| Telegram inbound Rich Messages | Formatted pastes reach the agent as Markdown; unclaimed message families are logged, not dropped | Telegram handler registration, inbound classification, mention sources, or reply context changes | [Telegram inbound Rich Messages](maintenance/telegram-inbound-rich-messages.md) |
| Telegram topic titles and icons | Preserve configurable title generation, semantic Bot API topic icons, and duplicate visible labels via lineage aliases | Topic title/icon changes and every upstream sync touching title, Telegram, or session state | [Telegram topic titles and icons](maintenance/telegram-topics.md) |
| Link-informed session titles | The first link's page title/description informs the model title under strict, fail-soft fetch limits | Title input, title prompt, `url_safety` private-address, or link-fetch changes | [Link-informed titles](maintenance/title-link-context.md) |
| Copilot ACP usage-less compaction | ACP responses report unknown usage, never fabricated zeros, so estimate-driven compaction still fires | Copilot ACP client response or usage accounting changes | [Copilot ACP usage](maintenance/copilot-acp-usage.md) |
| Nightly concurrency and compaction regressions | Preserve early process output, tolerate concurrent DB quarantine, and exercise actual ACP summarization | Process heartbeat, state DB preflight/quarantine, or ACP compaction E2E changes | [Nightly regression repairs](maintenance/nightly-regression-0926b.md) |
| Foreground command exit cleanup | Fence graceful exit against spawning foreground commands so no child outlives its host | Foreground spawn publication, terminal environment process-exit cleanup, or live foreground killing changes | [Foreground exit cleanup](maintenance/foreground-exit-cleanup.md) |
| Snapshot keeps shell functions | Functions and aliases survive every command of a terminal session, not just the first | Terminal session snapshot bootstrap or per-command re-dump changes | [Snapshot functions](maintenance/snapshot-keep-functions.md) |
| Explicit topic title receipts | Report the stored alias and actual Bot API rename result | `/title` Telegram topic changes and upstream title sync | [Explicit title receipts](maintenance/telegram-title-receipts.md) |
| Cron fallback routing | Keep scheduled agents' backup chain independent of interactive routing | Changing cron/provider resolution or evaluating an upstream release | [Cron fallback routing](maintenance/cron-fallback-routing.md) |
| Cron restart survival | Keep launchd-managed macOS cron workers alive through gateway process-group termination | Cron external worker dispatch, liveness recovery, or launchd restart changes | [Cron restart survival](maintenance/cron-restart-survival.md) |
| Immutable releases S2 | Pin code and venv to immutable releases behind an atomic pointer; preserve migration and rollback receipts | Update, release staging, launchd path resolution, cron worker pins or retention changes | [Immutable releases and activation runbook](maintenance/seamless-restart-s2.md) |
| Telegram ingress non-blocking | The update consumer never waits on outbound pacing, the pending-update probe requires no dispatch progress, and reconnect waits for the old poller to release | Telegram update handlers, busy or inline command replies, the heartbeat pending probe, or poller token ownership | [Telegram ingress non-blocking](maintenance/telegram-ingress-nonblocking.md) |
| Stdio wrapper chain | Agent builds and thread-scoped silencing never stack or loop `sys.stdout`/`sys.stderr` wrappers, and wrapper attribute lookup cannot recurse | `_SafeWriter`, `_install_safe_stdio`, `thread_scoped_output`, or other process-lifetime stdio rebinding | [Stdio wrapper chain](maintenance/stdio-wrapper-chain.md) |
| Telegram delivery | Preserve flood coherence, split-send recovery, and legacy emphasis | Telegram send/edit/typing, delivery ledger, or emphasis changes | [Telegram delivery](maintenance/telegram-delivery.md) |
| Telegram stale final delivery | Recover completed replies from deleted private DM topics without duplicating partially sent content or moving interim output | Telegram private-topic final sends and recovery | [Telegram stale final delivery](maintenance/telegram-stale-final-delivery.md) |
| Telegram internal delivery recovery | Redeliver failed answers on same-adapter polling recovery without crossing profile ownership | Polling health, final delivery settlement, or runtime ledger replay | [Telegram internal delivery recovery](maintenance/telegram-internal-delivery-recovery.md) |
| Status bubble ownership and cleanup | Keep Telegram topic/turn statuses independent and forget deleted bubbles | Telegram/Slack `send_or_update_status`, `delete_message`, or progress cleanup changes | [Status cache after cleanup](maintenance/status-cache-after-cleanup.md) |
| Per-turn progress cleanup | Keep queued callback ownership isolated and await progress deletion before the next turn | Post-delivery callback registration, delivery handoff, or progress cleanup changes | [Per-turn progress cleanup](maintenance/progress-cleanup-turns.md) |
| Outbox coalesced sweep | Deferred outbox redelivery runs as one sweep per store and adapter identity, with serialized recovery, closed connections, durable receipts and held rows logged once | Outbox retry scheduling, `recover()`, receipts, held-row reporting or store connections | [Outbox coalesced sweep](maintenance/outbox-coalesced-sweep.md) |
| Progress bubble flood deferral | A flood-refused progress edit retries later instead of turning every later tool line into its own message | Progress overflow split, send-or-edit tick, or progress edit failure classification changes | [Progress bubble flood deferral](maintenance/progress-overflow-flood-defer.md) |
| Restart continuation | Let interactive platforms continue interrupted work after a gateway restart | Restart recovery, resume notes, config bridging, or adapter resume defaults | [Restart continuation](maintenance/restart-continuation.md) |
| Restart notices | Scope shutdown notices to conversation lanes, preserve interim streams, and describe the actual resume policy | Shutdown advisories, home-channel broadcast, or restart notice wording | [Restart notices](maintenance/restart-notices.md) |
| Restart verification truth | Cover cron drain in the bounded observer wait, preserve outgoing inventory PIDs, and treat saved obligations as history | CLI update/restart wait, fleet snapshot, or pending warning changes | [Restart verification truth](maintenance/restart-verification-truth.md) |
| Delegation restart drain | Planned restarts wait for live background delegations, and interrupted children report why they stopped | Restart wait, shutdown drain accounting, CLI exit-wait budget, or child interrupt reporting | [Delegation restart drain](maintenance/delegation-restart.md) |
| Delegation service tier | Opt-in inheritance of the parent's Fast mode (route-independent, re-derived for the child's route) and of an explicit session reasoning pick, plus the `capabilities.fast_mode` custom-provider opt-in | `delegation.inherit_service_tier`, `capabilities.fast_mode`, `delegation.reasoning_effort`, `agent.reasoning_override` or child runtime/request-override resolution | [Delegation service tier](maintenance/delegation-service-tier.md) |
| Bounded delegation notices | Completion notices echo only a bounded head of the dispatch context | Async-delegation notice rendering changes | [Bounded delegation notices](maintenance/bounded-delegation-notices.md) |
| Plugin-claimed failure notices | A plugin that recovers a child failure (review fallback) can suppress the premature "Subagent failed" notice, decided once per failure | Subagent failure notice or `subagent_failure_notice` hook changes | [Review fallback notice](maintenance/review-fallback-notice.md) |
| Hygiene prompt not reused | A prompt persisted by memory-only gateway hygiene compaction is rebuilt on the next real turn, not adopted as a surface | Stored-prompt restore or hygiene compaction prompt handling | [Hygiene prompt not reused](maintenance/hygiene-prompt-not-reused.md) |
| Gateway commands while busy | Preserve alias expansion and defer-until-idle on the busy path | Busy-session guards, `quick_commands`, or slash-command admission changes | [Gateway commands](maintenance/gateway-commands.md) |
| Background topic recovery | Keep detached `/bg` and `/btw` answers in recovered Telegram DM topics without cross-topic reply anchors | `/bg`, `/btw`, or Telegram DM-topic source routing changes | [Background topic recovery](maintenance/background-topic-recovery.md) |
| Live gateway inference controls | Apply busy `/reasoning` and `/fast` at the live agent's next model request without eviction | Gateway inference controls, agent request overrides, or cache wiring | [Live inference controls](maintenance/live-inference-controls.md) |
| Session-scoped Fast expiry | Switch session `/fast fast` and `/fast ultrafast` overrides to explicit normal after a configurable wall-clock deadline | `agent.fast_expiry_seconds`, session tier resolution, cached-agent request overrides, or Fast status/notice changes | [Session-scoped Fast expiry](maintenance/session-fast-expiry.md) |
| Queued voice transcription | Transcribe and echo queued voice immediately with bounded concurrency, reuse it at drain | Busy queueing, pending-event STT cache, transcript echo, or drain changes | [Queued voice STT](maintenance/queued-voice-stt.md) |
| Worktree GC Git isolation | Host Git config never hides files from the reclaim safety check; every listed path is archived exactly or the worktree is kept | Worktree-GC dirty checks, archiving, or reclaim changes | [Worktree GC Git isolation](maintenance/worktree-gc-git-isolation.md) |
| Camofox accounts and vault | Preserve named accounts, Connect/secret-safe fills, shadow-DOM login forms | Browser account, vault, or 1Password backend changes | [Camofox and vault](maintenance/camofox-vault.md) |
| Camofox navigation titles | Return the exact owned tab's title without crossing account boundaries | Camofox navigation or tab-list changes | [Camofox navigation titles](maintenance/camofox-navigation-titles.md) |
| Browser upload | Attach local files to the page's upload control, including cross-origin iframes, with safe path checks and staging | Camofox tab actions, upload route, or `uploads_dir` changes | [Browser upload](maintenance/browser-upload.md) |
| Slack ordered list numbering | Preserve authored starts across paragraphs, bullets, and nesting | Slack rich-text list parsing, grouping, or outbound Block Kit rendering changes | [Slack ordered list numbering](maintenance/slack-ordered-list-numbering.md) |
| Slack status on the legacy API | Thread status and its clear stay on `assistant.threads.setStatus`; titles may use Agent Sessions | Slack status/title calls or upstream Agent Sessions changes | [Slack status legacy](maintenance/slack-status-legacy.md) |
| Agent secret entry | The agent may type a self-fetched password when the vault has no item for the origin, and a self-fetched code when the vault cannot mint one; values shown on a page or in chat never count | Vault tool descriptions, the browser input vault note, or upstream vault prompt changes | [Agent secret entry](maintenance/agent-secret-entry.md) |
| Security guidance plugin | Keep bounded path-aware security pattern guidance and explicit warning/block semantics | Security-guidance pattern, plugin wiring, or focused-test changes | [Security guidance plugin](maintenance/security-guidance.md) |
| Essential skill opt-out | Let one home opt out of seeding and protecting the essential `hermes-agent` skill | Bundled or essential skill seeding, disabled-list, or delete-guard changes | [Essential skill opt-out](maintenance/essential-skill-opt-out.md) |
| Background review memory delete | Opt-in lets the unattended review fork replace/remove memory instead of staging | Background-review memory gate or `memory` config changes | [Background review memory delete](maintenance/background-review-memory-delete.md) |
| Skill observation files | One immutable `<skill>@<suffix>.md` file per observation, indexed and archived independently | Observation store, `hermes observations`, or observation writers change | [Skill observation files](maintenance/skill-observation-files.md) |
| Profile plugins and skills | Keep Hermes runtime contracts and skill ownership boundaries explicit while profile plugin source remains external | Profile-plugin integration, skill guard, or curation changes | [Profile plugins](maintenance/profile-plugins.md) |
| Wrapped gateway service ownership | Protect service-owned gateway descendants from updater and reaper manual sweeps | Service PID discovery, updater restart, or wrapper changes | [Wrapped gateway ownership](maintenance/wrapped-gateway-ownership.md) |
| Launchd restart verification | Start the respawn window after the old gateway exits, so a slow drain is not a failed restart | Updater launchd restart verification or shutdown budget changes | [Launchd verify after shutdown](maintenance/launchd-verify-after-shutdown.md) |
| Backup, state, and tooling | Truthful backups, schema rehearsal, per-job timezone, fork maintenance scripts | Backup, cron scheduling, context ports, or maintenance script changes | [Backup and tooling](maintenance/backup-and-tooling.md) |
| Quick snapshot recovery | Bound partial captures while retaining verified recovery generations | Quick-snapshot capture, pruning, or restore changes; read Backup and tooling too | [Quick snapshot recovery](maintenance/quick-snapshot-recovery.md) |
| Hindsight memory provider | Keep the provider constructible before `initialize()`, so a construction error cannot silently disable retain and recall | Memory plugin lifecycle, retain strategy, or cron-exclusion changes | [Hindsight memory](maintenance/hindsight-memory.md) |
| Auxiliary overload fallback | Route status-less provider overloads through configured auxiliary fallback | Auxiliary error classification or fallback-chain changes | [Auxiliary overload fallback](maintenance/auxiliary-overload-fallback.md) |
| Compression route deadline | Preserve auxiliary compression fallback at the shared route deadline | Compression worker cancellation, deadline, or fallback changes | [Compression route deadline](maintenance/compression-route-deadline.md) |
| Manual compression levels | `/compress --level 1-3` escalation and a `here N` head that keeps no extra tail | `/compress` parsing, `here N`, or manual compression budget changes | [Manual compression levels](maintenance/manual-compression-levels.md) |
| Long request turn split | Split an oversized in-progress turn even when its opening request is long, so goal runs stay compressible | Oversized-turn split gates or in-flight request restatement changes | [Long request turn split](maintenance/long-request-turn-split.md) |
| Oneshot plugin-hook discovery | Complete background plugin discovery before first-turn lifecycle hook delivery so `pre_llm_call` cannot observe a partial registry | Plugin startup discovery barriers, oneshot hook delivery, or plugin registry readiness | [Oneshot plugin-hook discovery](maintenance/oneshot-plugin-hook-discovery.md) |
| MCP caller identity | Opted-in MCP servers receive the calling session's ContextVar identity as per-call request `_meta`, never the model's arguments or `os.environ` | MCP tool-call dispatch, per-server opt-ins, or session identity reads for MCP | [MCP caller identity](maintenance/mcp-caller-identity.md) |
| Release defects | Narrow, guarded fixes for defects found while syncing to `v2026.9.24`, each with a patch identity and guard test | Before changing a file a section names, when a sync review finds a defect, or when checking whether upstream now fixes one | [Release defects](maintenance/release-defects.md) |
| Direct web extraction and local docs | Bounded, safe direct fetches and checkout-backed docs avoid paid provider calls | Web extraction routing, URL safety, docs mapping, or extract config changes | [Direct web extraction](maintenance/web-extract-direct.md) |

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
`Fork-Patch-Backfill: <stable-patch-id>; <owned-identity>` line. This covers only
the reviewed patch content without rewriting shared history or advancing the floor.
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
