# Validated stable pointer (owned fork policy)

`main` continues syncing upstream. `stable` is only a pointer to an exact validated
ancestor of `main`, never a divergent release-development branch. Nothing in this
policy creates a scheduler, a second review/CI gate, or automatic rollback.

## Promotion admission: existing maintenance owner

Use the existing required tests, review receipts, and maintenance
observation records. The **promotion owner**, not an updater reading a branch
name, is responsible for proving eligibility. A commit date, a branch name, a
successful fetch, or an empty blocker list alone is not proof.

Local or fork-only patches and upstream promotions share tests, review, and
ancestry. They do not share settling time. Review means an accepted review of
the exact candidate. It does not require a separate reviewer, provider, or
independence label.

1. Freeze the owned remote `main` SHA. Enumerate its ancestors newest-first with
   `git rev-list --topo-order <main-sha>` (descendants precede ancestors; Git's
   ordering breaks ties between independent histories). Never develop on stable.
2. Inspect candidates in that order. Select the first whose **exact SHA** has all
   required tests passing and accepted review, with no unresolved
   blocking findings. Use original receipts/logs and verify their SHA/tree scope;
   changed files, stale review scope, mismatched SHA, or missing original evidence
   disqualify a candidate. Never manufacture historical eligibility.
3. **Upstream promotions** also require at least **24 hours of recorded settling
   with no known regressions**. Retain the observed start/end timestamps and
   operational evidence in the existing maintenance record. Commit age alone is
   insufficient. At exactly 24 hours, the age condition is satisfied; even one
   microsecond short is not. Refresh known blocker/regression sources immediately
   before promotion. A new blocking regression invalidates eligibility regardless
   of prior green tests. **Local or fork-only patches skip this age gate.** An
   **explicit urgent authorization naming the validated SHA** may waive settling
   time for an upstream promotion only.
4. Hosted CI unavailable because of billing/spending limits is **unavailable**, not
   green. Already-approved equivalent local checks may qualify only with exact
   candidate SHA, commands, complete results, environment, artifacts, and original
   equivalence approval recorded. Skipped tests remain visible gaps; an updater
   success receipt is not a review or CI substitute.
5. Urgent authorization never waives tests, review, unresolved blocking findings,
   current regression checks, or ancestry. Record the authority, reason and waived
   age in the same maintenance record. `--yes`, `--force`, an update request, or an
   urgent label without exact-SHA authorization is not this exception. The existing
   `--revision <SHA>` installs an explicitly approved urgent target without
   introducing another override switch or approval system.
6. If no candidate qualifies, preserve the current runtime, report exactly which
   evidence is missing, and do not create or move stable. Do not point live config
   at nonexistent/unqualified stable. An older stable never authorizes downgrading
   a newer working installation automatically.

### Publishing the owned pointer

Confirm there is no conflicting owner and read the exact current
`refs/heads/stable`. Reconfirm candidate ancestry against the current owned main,
check refreshed regression sources, and update only the owned remote's stable
ref. Use an exact-old-SHA lease for an existing pointer; an empty expected value
asserts absence for first creation:

```sh
git push --force-with-lease=refs/heads/stable:<observed-old-sha-or-empty> origin \
  <eligible-exact-sha>:refs/heads/stable
git ls-remote origin refs/heads/stable
```

The lease is compare-and-swap protection, not permission to downgrade a runtime
or override another owner. Stop on conflict. Read back the exact target before
claiming publication. Record the candidate, frozen main, prior/new pointer,
original test/review/settling evidence, refreshed regression sources, and any
urgent authorization in the existing maintenance record. Never push upstream.

No new evidence format or controller is needed: existing test/review/maintenance
artifacts remain authoritative. The source updater cannot prove operational
history from Git, and intentionally does not pretend that it can.

## Installation: existing immutable updater

After this source is delivered **and** a qualified owned stable pointer exists,
the top-level runtime owner may opt in:

```yaml
updates:
  channel: stable
  pre_update_backup: quick
```

`updates.channel` defaults to `main`; explicit `--branch` or `--revision` wins.
`hermes update --branch stable` is the one-shot equivalent. Ordinary feature
branches retain their existing behavior. `--plan` still inventories the fleet;
`--check` reports branch availability, not promotion eligibility.

The updater resolves remote main/stable together once, freezes both exact SHAs,
and fetches/uses only those objects. Shallow checkouts use supported
`git fetch --unshallow origin <frozen-main> <frozen-stable>` to remove ancestry
boundaries; history retrieval failure stops the attempt, never re-resolves tips.
Dirty working trees are rejected before network access. It does not consult `FETCH_HEAD` or a moving
remote-tracking ref for target identity. It verifies stable is an ancestor of main
and the current installation is an ancestor of stable, refusing missing stable,
divergent history, and implicit downgrade. Later pointer movement cannot retarget
this attempt. Dirty-tree/compatibility checks, source rollback retention,
dependency installation, receipt recording, checkout checks and live-runtime
verification reuse the existing `--revision` path; ZIP fallback is disabled.

A quick state snapshot is mandatory for stable: `--no-backup`, disabled backup,
or snapshot failure prevents source checkout. The previous source SHA is retained
under `refs/hermes-update-backups/...`, with prior dependency manifests and the
existing prepared/completed revision receipts. Snapshot ID and chosen SHA are
recorded in the normal update receipt. Do not confuse checkout success with live
runtime success: only the top-level owner may activate/restart and confirm the
reported running code SHA equals the frozen target.

### Rollback limits

The retained ref preserves **source**, not a byte-for-byte previous environment.
Dependency manifests help reconstruction but do not preserve installed wheels,
external services, or native binaries. Quick snapshots cover existing selected
critical state and have size/coverage limits; they are not a transactional backup
of every file or external system. Neither a Git checkout nor reinstalling old
dependencies reverses database/config migrations. Before rollback, reconcile the
snapshot and migrations, retained manifests, runtime-compatibility checks, and
irreversible/external effects. Restore source and state coherently under explicit
runtime-owner control; no blind `git reset` or automatic downgrade.

## Regression cases

Automated in `tests/hermes_cli/test_update_stable.py` and the existing immutable
revision tests:

- remote main/stable move after initial resolution: the exact original SHA is
  still installed/verified, without shared `FETCH_HEAD` identity;
- missing stable, divergent release history, or downgrade: refuse, preserve HEAD;
- configured stable and one-shot selection use the pinned pipeline;
- backup disabled or failed: no source checkout;
- rollback ref and receipt retain the prior SHA and manifests.

Mandatory promotion-owner acceptance cases, evaluated against existing evidence
rather than a duplicate test/review framework:

- newest candidate fails a gate: select the newest older fully eligible ancestor,
  or no candidate; never substitute an arbitrary old commit;
- stale/mismatched test or review evidence: ineligible until corrected originals
  prove exact-SHA coverage;
- local or fork-only patch: tests and review required; 24-hour settling does not apply;
- 23:59:59.999999 settling on an upstream promotion: ineligible; 24:00:00: age
  passes only if all other gates pass and observations show no known regressions;
- new blocking regression after validation: eligibility revoked before promotion;
- explicit urgent exact-SHA authorization: upstream settling age may be skipped,
  never tests, review, blockers, ancestry, or safe installation;
- rollback record absent/unreadable or snapshot failure: do not checkout.

Existing scheduler/helper installation, source findings, and real cron
verification remain separate open maintenance obligations; this policy closes
none of them.
