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

The personal profile's script jobs use copies under `$HERMES_HOME/scripts`.
`scripts/install_candidate_extensions.py` owns that copy operation. Review the
source revision, installed copies, actual job arguments, and job receipt together
when changing a procedure. A Git update alone does not refresh these copies.

| Job | Canonical procedure | Invocation contract and verification |
|---|---|---|
| `sync-fork-candidate` | `scripts/sync_fork_candidate.py` | Host wrapper supplies `--repo`, `--candidate`, and `--current-base`. Use a dedicated sync worktree. Inspect rebased SHA and tests; `--publish` publishes only a candidate. |
| `check-fork-patches` | `scripts/check_fork_patches.py` | Resolve installed package and intended `--home`; inspect trailer, unit-ownership, and registration results plus actual runtime evidence. |
| `curate-skill-observations` | `scripts/curate_skill_observations.py` | Host wrapper supplies a dedicated `--dotfiles` worktree. Without `--publish`, it validates then restores staging. That is not a published curation change. |
| `snapshot-profile-state` | `candidate-profile/snapshot_profile_state.sh` | Verify the output manifest and isolated restoration. This small-state procedure does not establish a full `state.db` backup. |

Schedules, delivery targets, and enabled state live in the profile's cron store.
Do not recreate jobs from this document or add a competing scheduler. The source
sync and curation wrappers inspected on 2026-09-19 omit `--publish`; their successful
dry runs do not prove an automatic promotion or PR workflow. Company updates
continue through their signed release owners, not these personal jobs.

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
