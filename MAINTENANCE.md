# Hermes fork maintenance

## Background

Canonical source: `/Users/brianle/Repos/hermes-agent`, published as
[`0xble/hermes-agent`](https://github.com/0xble/hermes-agent), branch `main`.
Upstream is [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent),
default branch `main`, remote `upstream-live`. Accepted release baseline:
`v2026.9.24 + qualified checkpoint`, `ca6782850432927f33df4775cb6dd45bb51460d2`.
Brian explicitly selected this exact upstream checkpoint on 2026-09-25 for
one sync and personal-runtime promotion. It contains released baseline
`v2026.9.24` (`f97608f178d1ffeca59860195ab7da295f7c8e5f`) plus 256 upstream
commits, with successful upstream CI run `36022678358` and over 24 hours of age.
This one-run non-release exception does not change recurring release selection
or waive fork tests, independent review, protected landing, or backup safeguards.
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
| Fork CI | Reproducible patch proof surfaces and complete-suite access on bounded runners | CI, test harness, or proof-surface changes | [Fork CI](maintenance/fork-ci.md) |
| Goal lifecycle | Complete judge criteria and conversational recovery of blocker pauses | Goal judging, admission, or continuation changes | [Goal lifecycle](maintenance/goal-lifecycle.md) |
| Telegram rendering | Preserve rich mode selection and prompt/delivery agreement | Telegram rendering changes and every upstream sync; also load runtime ownership before promotion | [Telegram rendering](maintenance/telegram-rendering.md) |
| Telegram inbound Rich Messages | Formatted pastes reach the agent as Markdown; unclaimed message families are logged, not dropped | Telegram handler registration, inbound classification, mention sources, or reply context changes | [Telegram inbound Rich Messages](maintenance/telegram-inbound-rich-messages.md) |
| Telegram topic titles and icons | Preserve configurable title generation, semantic Bot API topic icons, and duplicate visible labels via lineage aliases | Topic title/icon changes and every upstream sync touching title, Telegram, or session state | [Telegram topic titles and icons](maintenance/telegram-topics.md) |
| Cron fallback routing | Keep scheduled agents' backup chain independent of interactive routing | Changing cron/provider resolution or evaluating an upstream release | [Cron fallback routing](maintenance/cron-fallback-routing.md) |
| Telegram delivery | Preserve flood coherence, split-send recovery, and legacy emphasis | Telegram send/edit/typing, delivery ledger, or emphasis changes | [Telegram delivery](maintenance/telegram-delivery.md) |
| Telegram internal delivery recovery | Redeliver failed answers on same-adapter polling recovery without crossing profile ownership | Polling health, final delivery settlement, or runtime ledger replay | [Telegram internal delivery recovery](maintenance/telegram-internal-delivery-recovery.md) |
| Status bubble cache after cleanup | Forget deleted status bubbles so the next status sends fresh instead of editing a gone message | Telegram/Slack `send_or_update_status`, `delete_message`, or progress cleanup changes | [Status cache after cleanup](maintenance/status-cache-after-cleanup.md) |
| Restart continuation | Let interactive platforms continue interrupted work after a gateway restart | Restart recovery, resume notes, config bridging, or adapter resume defaults | [Restart continuation](maintenance/restart-continuation.md) |
| Delegation restart drain | Planned restarts wait for live background delegations, and interrupted children report why they stopped | Restart wait, shutdown drain accounting, CLI exit-wait budget, or child interrupt reporting | [Delegation restart drain](maintenance/delegation-restart.md) |
| Delegation service tier | Opt-in inheritance of the parent's Fast preference, re-derived for the child's route | `delegation.inherit_service_tier` or child request-override resolution | [Delegation service tier](maintenance/delegation-service-tier.md) |
| Bounded delegation notices | Completion notices echo only a bounded head of the dispatch context | Async-delegation notice rendering changes | [Bounded delegation notices](maintenance/bounded-delegation-notices.md) |
| Plugin-claimed failure notices | A plugin that recovers a child failure (review fallback) can suppress the premature "Subagent failed" notice, decided once per failure | Subagent failure notice or `subagent_failure_notice` hook changes | [Review fallback notice](maintenance/review-fallback-notice.md) |
| Hygiene prompt not reused | A prompt persisted by memory-only gateway hygiene compaction is rebuilt on the next real turn, not adopted as a surface | Stored-prompt restore or hygiene compaction prompt handling | [Hygiene prompt not reused](maintenance/hygiene-prompt-not-reused.md) |
| Gateway commands while busy | Preserve alias expansion and defer-until-idle on the busy path | Busy-session guards, `quick_commands`, or slash-command admission changes | [Gateway commands](maintenance/gateway-commands.md) |
| Queued voice transcription | Transcribe and echo queued voice immediately, reuse it at drain | Busy queueing, pending-event STT cache, transcript echo, or drain changes | [Queued voice STT](maintenance/queued-voice-stt.md) |
| Camofox accounts and vault | Preserve named accounts, Connect/secret-safe fills, shadow-DOM login forms | Browser account, vault, or 1Password backend changes | [Camofox and vault](maintenance/camofox-vault.md) |
| Security guidance plugin | Keep bounded path-aware security pattern guidance and explicit warning/block semantics | Security-guidance pattern, plugin wiring, or focused-test changes | [Security guidance plugin](maintenance/security-guidance.md) |
| Essential skill opt-out | Let one home opt out of seeding and protecting the essential `hermes-agent` skill | Bundled or essential skill seeding, disabled-list, or delete-guard changes | [Essential skill opt-out](maintenance/essential-skill-opt-out.md) |
| Background review memory delete | Opt-in lets the unattended review fork replace/remove memory instead of staging | Background-review memory gate or `memory` config changes | [Background review memory delete](maintenance/background-review-memory-delete.md) |
| Skill observation files | One immutable `<skill>@<suffix>.md` file per observation, indexed and archived independently | Observation store, `hermes observations`, or observation writers change | [Skill observation files](maintenance/skill-observation-files.md) |
| Profile plugins and skills | Keep Hermes runtime contracts and skill ownership boundaries explicit while profile plugin source remains external | Profile-plugin integration, skill guard, or curation changes | [Profile plugins](maintenance/profile-plugins.md) |
| Wrapped gateway service ownership | Protect service-owned gateway descendants from updater and reaper manual sweeps | Service PID discovery, updater restart, or wrapper changes | [Wrapped gateway ownership](maintenance/wrapped-gateway-ownership.md) |
| Launchd restart verification | Start the respawn window after the old gateway exits, so a slow drain is not a failed restart | Updater launchd restart verification or shutdown budget changes | [Launchd verify after shutdown](maintenance/launchd-verify-after-shutdown.md) |
| Backup, state, and tooling | Truthful backups, schema rehearsal, per-job timezone, fork maintenance scripts | Backup, cron scheduling, context ports, or maintenance script changes | [Backup and tooling](maintenance/backup-and-tooling.md) |
| Hindsight memory provider | Keep the provider constructible before `initialize()`, so a construction error cannot silently disable retain and recall | Memory plugin lifecycle, retain strategy, or cron-exclusion changes | [Hindsight memory](maintenance/hindsight-memory.md) |
| Release defects | Narrow, guarded fixes for defects found while syncing to `v2026.9.24`, each with a patch identity and guard test | Before changing a file a section names, when a sync review finds a defect, or when checking whether upstream now fixes one | [Release defects](maintenance/release-defects.md) |

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
