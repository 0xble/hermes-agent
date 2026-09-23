# Hermes fork maintenance

## Background

Canonical source: `/Users/brianle/Repos/hermes-agent`, published as
[`0xble/hermes-agent`](https://github.com/0xble/hermes-agent), branch `main`.
Upstream is [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent),
default branch `main`, remote `upstream-live`. Accepted release baseline:
`v2026.9.21`, `d337b736aa1e8ebecfab043842d13e4a2d2f48a3`.
Explicit upstream exception authorized on 2026-09-23: adopt through
`38c289c0146ed8c8b2b767eca1fff6f5b7e6382e` for native Codex GPT-6 Sol/Luna.
This cutoff includes registration (`79ec1f2a34a5b58d91ee835488490a8fbce007b8`),
live catalog discovery (`1d10cef8363aa3148d70ed162d0a1fc809347182`), and removal
of unpublished Terra. The user waived aging for this adoption, not verification,
independent review, protected landing, or backup safeguards. Later Desktop picker
and generic provider-cache changes are outside this native Codex adoption.
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
| Telegram topic titles and icons | Preserve configurable title generation, semantic Bot API topic icons, and duplicate visible labels via lineage aliases | Topic title/icon changes and every upstream sync touching title, Telegram, or session state | [Telegram topic titles and icons](maintenance/telegram-topics.md) |
| Cron fallback routing | Keep scheduled agents' backup chain independent of interactive routing | Changing cron/provider resolution or evaluating an upstream release | [Cron fallback routing](maintenance/cron-fallback-routing.md) |
| Telegram delivery | Preserve flood coherence, split-send recovery, and legacy emphasis | Telegram send/edit/typing, delivery ledger, or emphasis changes | [Telegram delivery](maintenance/telegram-delivery.md) |
| Restart continuation | Let interactive platforms continue interrupted work after a gateway restart | Restart recovery, resume notes, config bridging, or adapter resume defaults | [Restart continuation](maintenance/restart-continuation.md) |
| Delegation restart drain | Planned restarts wait for live background delegations, and interrupted children report why they stopped | Restart wait, shutdown drain accounting, CLI exit-wait budget, or child interrupt reporting | [Delegation restart drain](maintenance/delegation-restart.md) |
| Gateway commands while busy | Preserve alias expansion and defer-until-idle on the busy path | Busy-session guards, `quick_commands`, or slash-command admission changes | [Gateway commands](maintenance/gateway-commands.md) |
| Camofox accounts and vault | Preserve named accounts, Connect/secret-safe fills, shadow-DOM login forms | Browser account, vault, or 1Password backend changes | [Camofox and vault](maintenance/camofox-vault.md) |
| Candidate extensions and skills | Keep the personal plugins registering through discovery and skill ownership enforced | Extension, installer, skill guard, or curation changes | [Candidate extensions](maintenance/candidate-extensions.md) |
| Wrapped gateway service ownership | Protect service-owned gateway descendants from updater and reaper manual sweeps | Service PID discovery, updater restart, or wrapper changes | [Wrapped gateway ownership](maintenance/wrapped-gateway-ownership.md) |
| Backup, state, and tooling | Truthful backups, schema rehearsal, per-job timezone, fork maintenance scripts | Backup, cron scheduling, context ports, or maintenance script changes | [Backup and tooling](maintenance/backup-and-tooling.md) |
| Hindsight memory provider | Keep the provider constructible before `initialize()`, so a construction error cannot silently disable retain and recall | Memory plugin lifecycle, retain strategy, or cron-exclusion changes | [Hindsight memory](maintenance/hindsight-memory.md) |

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
