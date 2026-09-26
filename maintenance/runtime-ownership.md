# Runtime ownership and recovery

Use when installation, update/rollback tooling, scheduled procedures, or recovery
evidence changes. [The root contract](../MAINTENANCE.md) owns source adoption.
Before acting, resolve the actual host, profile home, launcher, source revision,
and existing recovery point. The paths below distinguish owners, not current
health claims.

## Installation boundaries

| Surface | Owner and invariant | Required proof after an authorized change |
|---|---|---|
| Personal source | `~/Repos/hermes-agent`, owned fork `main` | Published and local source identity agree. |
| Personal runtime | `~/.hermes/hermes-agent`, launchd-supervised gateway, profile `~/.hermes` | Read running-code identity, profile, and supervisor state independently of source HEAD. |
| LPG runtime | `~/Repos/lpg/apps/agent` signed release route, host `/opt/agent/current` and `/opt/hermes/current` | Verify both agent release and runtime artifact identities, company overlay, and supervisor. |
| Meridian runtime | `~/Repos/meridian-next/apps/agent` signed release route | Verify agent/runtime release chain on-host. The old standalone `meridian-agent` repository is not the release owner. |

Company releases preserve their own model policy, Hindsight banks (`lpg` and
`meridian`), skills libraries, and browser aliases. Personal policy uses bank
`brianle`. Neither company's September 19 cutover shipped the four candidate
extensions. Do not infer plugin acceptance from a runtime-source upgrade.
Read current company configuration from its release repository and live host;
the migration overlay snapshot is historical evidence, not current authority.

## Scheduled procedures

The personal profile's script jobs and optional sync helper use regular files under `$HERMES_HOME/scripts`.
`scripts/install_candidate_extensions.py --maintenance-only --home <profile>` installs
sync/verifier forwarding entry points without changing plugins or configuration.
When `<profile>/scripts` is a real directory, it preserves the existing flat
entrypoint paths. When it is a symlink to a domain-organized scripts checkout,
the three generated entrypoints live under `scripts/hermes/` instead; cron
references must be migrated to those paths separately before that cutover.
They execute procedures in `<profile>/hermes-agent/scripts`, so native promotion
updates their implementation too. Profile plugin source is maintained in the external
`agents` repository. Review
source revision, installed entry points, actual arguments, and receipts together.
The personal wrapper uses the permanent source checkout's `.venv` for tests. Its
prerequisites include pytest and the pinned lazy Hindsight client
(`uv pip install --python <source>/.venv/bin/python hindsight-client==0.6.1`).
It checks these imports before beginning and records each result under
`<profile>/maintenance/fork-sync/`. Missing dependencies are failures, not skipped tests.

| Job | Canonical procedure | Invocation contract and verification |
|---|---|---|
| `sync-hermes-fork` | Agent-owned source maintenance, with optional `scripts/sync_fork_candidate.py` helper | Use `gpt-6-astra`, `custom:codex-proxy`, low reasoning, agent mode, and no automatic script pre-run. In a dedicated linked worktree, integrate the selected upstream release, resolve conflicts, repair relevant failures, obtain required independent review, and land through the protected fork PR route. Inspect tested candidate, review evidence, and published `origin/main` readback. |
| `verify-hermes-fork` | `scripts/check_fork_patches.py` | Resolve installed package and intended `--home`; inspect trailer, unit-ownership, registration, and native receipt results plus actual runtime evidence. |
| `curate-skill-observations` | `scripts/curate_skill_observations.py` | Host wrapper supplies a dedicated `--dotfiles` worktree. Without `--publish`, it validates then restores staging. That is not a published curation change. |
| `snapshot-profile-state` | `candidate-profile/snapshot_profile_state.sh` | Verify the output manifest and isolated restoration. This small-state procedure does not establish a full `state.db` backup. |

Schedules, delivery targets, and enabled state live in the profile's cron store.
Edit the existing job through the supported cron interface. Do not recreate jobs
from this document or add a competing scheduler. The agent owns diagnosis and
repair even when the optional candidate builder stops on a conflict or failed test.
Helper publication to `candidate/<tag>` is not protected landing on fork `main`.
Release selection and ancestry follow the root contract. The main agent's low
reasoning pin does not override the independent review route's effort policy.

Source landing and runtime promotion remain separate. A source-only check does
not establish runtime health. Manually promote reviewed fork `main` through
`hermes update --yes` only with activation authority, retaining quick snapshots,
then check the native receipt, live identity, and a real model round trip.
Company updates continue through their signed release owners, not these personal jobs.

### Progress and failure handling

- Resume the existing candidate and inspect prior receipts, live process handles,
  and locks before starting work. Preserve unfinished conflict resolutions and
  valid exact-candidate test/review evidence. Do not reset progress each day.
- Record immutable release and candidate SHAs. Reconcile moving `origin/main`
  explicitly and revalidate affected evidence. Do not make unrelated changes to
  the primary checkout's HEAD invalidate a dedicated candidate, or restore the
  retired fork's guard/verifier machinery.
- Preflight credentials, test dependencies, and disk headroom before expensive
  work. Diagnose environmental failures separately from regressions. Confirm an
  upstream failure on the frozen release when needed, without weakening backup
  reserves, security checks, or branch protection to obtain a pass.
- Run long tests through the canonical runner using tracked background execution
  and a bounded completion budget. Keep process identity, logs, and exit status.
  A short foreground terminal timeout can kill the process tree. Dispatch or
  a surviving log file is not evidence of completion.
- When the same blocker recurs, inspect what changed before repeating expensive
  work. Change the diagnosis or repair strategy, or report the precise external
  prerequisite and next action. Keep the candidate checkpoint and failed evidence
  visible instead of producing an unchanged daily restart loop.
- Write a per-fire receipt under `<profile>/maintenance/fork-sync/` with run and
  candidate identity, completed stages, actual checks/review, remote readback,
  and unresolved blockers. Receipts are evidence, not a separate completion
  verifier. A blocked, deferred, or incomplete run must begin its final response
  with the exact standalone line `[CRON_FAILURE]`, followed by the diagnosis and
  next action. The native scheduler recognizes that first-line marker and records
  failure status and streaks. Do not hide an incomplete run with silence or a
  success summary. An already-current result still requires fresh release and
  remote identity checks.

## Recovery and acceptance

Preserve the personal capture at `~/.hermes-cutover-capture-20260919T050807Z`,
legacy installed checkout `~/.hermes/hermes-agent.legacy`, and code archive at
`~/Repos/archive/hermes-agent-legacy-20260919/` until independently verified
replacement recovery and acceptance permit retirement. Revalidate existence and
restore integrity before relying on these dated recovery locations. A Git bundle
preserves code, not profile databases, browser state, secrets, or external memories.
Company recovery must include the signed prior release and its matching state.

Before schema-affecting changes, exercise `scripts/schema_rehearsal.py` on a
consistent database copy. Compare row counts, search results, and fork-only data
as well as schema version. A successful open alone is insufficient.

Activation proof must include an actual inbound message and response on the host's
messaging platform, memory recall and an authorized reverted write, skills
discovery, browser identity and vault fill where configured, preserved cron
identities, update-plan ownership, and the agreed one-day sustained window.
A scheduled outbound message does not prove inbound handling. Account quota
failure is a blocked model-turn check, not a passing migration check.

Open limits from the September 19 evidence and source review remain until fixed
and verified: personal vault fill and true inbound acceptance were not demonstrated;
company model turns/round trips were quota-blocked; sustained windows were pending.
Source repairs for child-bound review receipts, reinstall-failure recovery, native
update receipt validation, and verified curation PR publication are present at
`acf05424e63d978ae3c4aafdb09e347e218e0098`. Their regression surface includes
`tests/scripts/test_candidate_scripts.py` and the review plugin tests. Verify the
installed copies carry those repairs before relying on them operationally. Source
repair does not close the outstanding runtime acceptance checks above.
