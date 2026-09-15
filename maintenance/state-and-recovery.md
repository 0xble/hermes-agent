# SQLite state, backup and recovery evidence

This responsibility covers SessionDB transaction ownership, corruption diagnosis and verified backup payload retention. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-001 | Retired |
| HERMES-002 | Active |
| HERMES-011 | Active |
| HERMES-032 | Active |
| HERMES-054 | Active |
| HERMES-055 | Retired |
| HERMES-079 | Active |
| HERMES-131 | Active |

## Patch records

### HERMES-001 — Retired malformed `state.db` repair serialization

- **Summary:** The private writable-schema repair lock, post-lock re-probe, schema-cookie bump, and duplicate fork tests have been removed. Released upstream commit `923d86e09` now owns the complete locking, re-probe, schema-cookie, hard-stop backup, and regression contract.
- **Surfaces:** Historical private surfaces were `hermes_state.py` and four patch-owned cases in `tests/test_state_db_malformed_repair.py`. The active replacement is upstream's repair implementation and tests in those same files.
- **Upstream tracking:** Replaced by released upstream commit `923d86e09`, the released descendant of the work previously tracked through PRs `#69609` and `#71982`.
- **Upstream PR:** Associated historical PRs: #69609 and #71982; released replacement commit: `923d86e09` (verified 2026-08-14).
- **Regression:** `scripts/run_tests.sh tests/test_state_db_malformed_repair.py` against the upstream-backed implementation.
- **Rollback:** Do not restore the private implementation or duplicate tests. If the upstream contract regresses, repair or backport upstream's coherent lock/re-probe/schema-cookie/hard-stop path; do not layer a second repair lock over it. HERMES-002's atomic raw-copy guard remains independent and must be preserved.
- **Additional historical subjects (optional provenance):** `chore(local): carry Brian-owned working-tree patches into the fork`; `docs(fork): retire state repair patch`.

### HERMES-002 — Make raw SQLite backup and quarantine connection-safe

- **Summary:** Holds `offline_file_access` across the live-connection check and byte-level copy/fingerprint operation, closing the check/use race that could cancel POSIX SQLite locks.
- **Surfaces:** `hermes_state.py`; `hermes_cli/kanban_db.py`; `tests/test_raw_copy_offline_guard.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/test_raw_copy_offline_guard.py`.
- **Rollback:** Remove the `_copy_all`/`offline_file_access` guarded backup path in `hermes_state.py` and `_backup_corrupt_db_locked` guarded quarantine path in `hermes_cli/kanban_db.py`, then remove `tests/test_raw_copy_offline_guard.py`. Preserve upstream's released HERMES-001 replacement and all unrelated database-repair behavior. Verify the upstream replacement with the same live-connection race cases before deleting the private test.
- **Additional historical subjects (optional provenance):** `chore(local): carry Brian-owned working-tree patches into the fork`; `fix(review): preserve reconciliation safety contracts`; `fix(state): guard directory fsync by platform`.

### HERMES-011 — Serialize SessionDB reads and diagnose persistence failures

- **Summary:** Routes the remaining unsafe public reads through `_read_ctx` and emits sanitized persistence diagnostics without unsafe retries. Upstream owns the pooled-reader lifecycle, hard peak-connection permit, borrowed-handle close guard, SessionStore borrowing, and process-wide shared registry.
- **Narrowed 2026-09-02:** Released upstream commit `db339f0051` consolidates gateway `SessionDB` writers through `get_shared_session_db()`. The rebase removed the now-dead fork `_resolve_shared_session_db` helper and its duplicate ownership tests, then restored upstream's registry recovery regression exactly. Fork retains only the four `_read_ctx()` public reads and sanitized persistence diagnostics.
- **Surfaces:** `hermes_state.py`; `run_agent.py`; `tests/test_sessiondb_cross_thread_safety.py`; persistence diagnostics in `tests/agent/test_run_agent.py`.
- **Upstream tracking:** The remaining private behavior is limited to the public-read and diagnostics contracts associated with upstream PRs `#73803` and `#78287`; deliberately excludes the fallback spool from `#78552`. Released upstream commits `87aedbe7b`, `0472c31aa`, and `db339f0051` own pooled readers, hard peak budgeting, and gateway handle consolidation.
- **Upstream PR:** Associated: #73803 and #78287 (open; checked 2026-08-14). Related but excluded: #78552.
- **Regression:** `scripts/run_tests.sh tests/test_sessiondb_cross_thread_safety.py tests/agent/test_run_agent.py -k 'persistence or sqlite or session_db or reader or writer'` plus upstream's `tests/gateway/test_session_db_recovery.py` and `tests/hermes_state/test_shared_session_db_registry.py`.
- **Rollback:** Remove only the remaining private public-read routing and sanitized diagnostics in a follow-up change while preserving upstream's registry, pooled-reader implementation, and later unrelated edits. Before full retirement, verify upstream covers all public-read serialization, sanitized diagnostics, and no duplicate-prone retry.
- **Additional historical subjects (optional provenance):** `fix(state): serialize public reads, bound readers, one gateway SessionDB`; `test(state): align shared SessionDB ownership regressions`; `fix(reconcile): restore upstream session-db and browser contracts`; `refactor(state): retire superseded gateway SessionDB ownership`.

### HERMES-032 — Make large-state diagnostics retention-aware

- **Summary:** Treats an oversized `state.db` as informational when `sessions.auto_prune` is enabled with a valid positive integer `retention_days`, including a numeric string. Disabled, invalid, or unavailable retention remains actionable. Pending or legacy FTS storage continues to recommend offline `hermes sessions optimize-storage`. Messaging states that pruning removes ended inactive sessions, does not cap active-session growth, and does not itself shrink the SQLite file.
- **Surfaces:** `hermes_cli/doctor.py`; `tests/test_state_db_stats.py`.
- **Upstream tracking:** Issue #83933 remains open and directly reports the false `auto_prune` recommendation. Open PR #83954 is a narrower associated fix. Closed-unmerged PR #84091 proposed a related retention-aware severity model. Open PR #86271 is broader health-diagnostic work and is not an equivalent replacement. No released upstream implementation touched the affected files as of 2026-08-17 at upstream `93ed11379b`.
- **Upstream PR:** Associated: #83954 (open, unmerged, no review decision; checked 2026-08-17). Related: #84091 (closed unmerged) and #86271 (open; checked 2026-08-17).
- **Regression:** `uv run pytest tests/test_state_db_stats.py tests/hermes_cli/test_doctor.py tests/hermes_cli/test_doctor_journal_modes.py -q`; `uv run ruff check hermes_cli/doctor.py tests/test_state_db_stats.py`; `git diff --check`; and a real `uv run hermes doctor` against an oversized retained database.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later unrelated Doctor changes. Remove `_session_retention_policy`, the explicit advisory issue field, retention-aware severity, and only HERMES-032's focused tests. Restore the prior tuple contract and issue construction without changing state-size, WAL, FTS, pruning, VACUUM, or runtime-maintenance behavior.
- **Retirement:** Retire after released upstream loads effective retention configuration, treats a large database with valid positive retention as informational, keeps invalid or unavailable retention and pending or legacy FTS actionable, avoids rendered-text issue inference, and passes the focused regressions plus a real Doctor canary.
- **Additional historical subjects (optional provenance):** `fix(doctor): make state db advisory retention aware`; `fix(reconcile): preserve cron and doctor contracts`.

### HERMES-054 — Attribute malformed errors before FTS repair

- **Independent hypothesis (2026-08-27):** `_is_fts_write_corruption_error` classifies ANY `database disk image is malformed` as FTS corruption. With genuine damage in a non-FTS table (live incident: `session_turn_leases` btree pages), the in-place rebuild "succeeds" against healthy indexes, the write fails again, the stale-FTS breadcrumb is set, and every process loops through multi-minute rebuilds while the real damage stays undiagnosed. The correction belongs at the repair boundary: before a generic-class malformed error may trigger a rebuild or stale marker, run FTS5's structure-only `integrity-check` (rank=0) against the present FTS tables; a clean probe routes the error to offline diagnosis (quick_check/dbstat guidance) instead. FTS5-specific messages skip the probe; a probe that cannot run preserves the historical fail-open behavior; results are cached for 60s.
- **Narrowed 2026-08-31:** upstream's `_is_fts_write_corruption_error` now requires SQLITE_CORRUPT_VTAB or an `fts5:` corrupt-structure message, so a bare `database disk image is malformed` is rejected before attribution runs. That is stricter than this patch and serves its own purpose better — marking FTS stale on unattributable damage is what drove the rebuild/watchdog crash-loop against a corrupt ephemeral lease btree. The fork's superseded "attribution-unknown keeps the historical fail-open" assertion was replaced; the retained fork behaviour is attribution for VTAB-scoped non-`fts5` errors, where an inconclusive probe still fails open.
- **Surfaces:** `hermes_state.py`; `tests/hermes_state/test_fts_runtime_rebuild.py`.
- **Upstream tracking:** Upstream's classifier and rebuild flow retain the generic-class attribution as of `36b0a96dcb` on 2026-08-27; no matching issue or PR found.
- **Upstream PR:** None after checked 2026-08-27.
- **Regression:** `.venv/bin/python -m pytest tests/hermes_state/test_fts_runtime_rebuild.py -q -k 'Attribution'`; the clean-probe case proves no rebuild and no stale marker for a non-FTS malformed error.
- **Rollback:** Remove `_fts_structure_is_corrupt` and its two call-site guards; preserve the rebuild, fail-open, and startup recovery flows.
- **Retirement:** Retire after released upstream positively attributes malformed errors to FTS structures before automatic index repair, with equivalent regressions.
- **Additional historical subjects (optional provenance):** `fix(reconcile): restore fork behaviour the v0.21.0 replay dropped`.

### HERMES-055 — Retired oversized runtime FTS cutoff

- **Status:** Retired source-level cutoff. No runtime deployment or retirement rollout is claimed by this documentation correction.
- **Independent hypothesis (2026-08-27):** The former unbounded runtime rebuild could hold the writer lock for minutes on large databases. The historical fix skipped runtime rebuilds above 1 GiB.
- **Current evidence (2026-09-06):** The old byte-size cutoff is absent. `hermes_state_search.py::_try_runtime_fts_rebuild` uses cross-process rebuild admission and defers on contention. `hermes_state_fts.py::_enter_fts_fail_open` preserves canonical writes while marking damaged indexes stale. HERMES-054 attribution and present rebuild safeguards remain unchanged.
- **Surfaces:** `hermes_state_search.py`; `hermes_state_fts.py`; `tests/hermes_state/test_fts_runtime_rebuild.py`; this record.
- **Upstream tracking:** Compared current source with the report and integrated upstream behavior. This corrects the obsolete register, not a new upstream-equivalence claim about every FTS safeguard.
- **Upstream PR:** None required for this fork-register correction.
- **Regression:** `scripts/run_tests.sh tests/hermes_state/test_fts_runtime_rebuild.py`.
- **Rollback:** Revert only this register correction if its evidence is disproven. Do not remove current FTS attribution, admission, fail-open, or rebuild code.
- **Retirement:** Historical size cutoff already absent at base `bcf269ec56d45c175ecbd7b83e166b946ed2fb65`. Do not reintroduce it merely to match the old record.
- **Additional historical subjects (optional provenance):** `fix(reconcile): restore fork behaviour the v0.21.0 replay dropped`; `fix(hermes): close verified fork improvement gaps (#72)`.

### HERMES-079 — Enforce application write patience

- **Summary:** Temporarily set the connection busy timeout to zero while acquiring the write transaction, restore it before invoking the write callback, roll back if timeout restoration fails after acquisition, and preserve the existing randomized retry, lock attribution, malformed-database recovery, compression-lease, and commit/rollback contracts.
- **Surfaces:** `hermes_state.py`; `tests/hermes_state/test_write_lock_patience.py`; this record.
- **Upstream tracking:** Existing upstream issue #74478 tracks lost session persistence under legitimate multi-process SQLite contention. Upstream `main` at `26350357d76e4508c8df9304a3374bdc5a6f6220` carries the application patience loop but still lets SQLite's busy handler overrun short budgets.
- **Upstream PR:** None confirmed for bounding `BEGIN IMMEDIATE` by the application patience deadline as of 2026-08-30.
- **Regression:** `scripts/run_tests.sh tests/hermes_state/test_write_lock_patience.py -q`; the exhausted-patience case must raise the attributed `OperationalError` in under one second while a two-second competing lock remains held, and the connection's configured busy timeout must be restored afterward. The complete file must continue proving long-lock survival for transcript-critical writes and uncontended performance.
- **Expected published commit identity:** Stable subject `fix(state): enforce application write patience`; source, regression, and this record ship together.
- **Rollback:** Revert only `fix(state): enforce application write patience`, restoring connection-level waiting during `BEGIN IMMEDIATE` and removing the elapsed-time and timeout-restoration assertions plus this record. No schema or persistent-data rollback is required.
- **Retirement:** Retire after a released upstream version proves that SQLite lock acquisition cannot outlive routine, transcript, or activity patience budgets, preserves attributed exhaustion errors and configured busy timeouts, and passes equivalent long-lock and short-budget regressions.
- **Source references from initial investigation:** ` first enters SQLite's connection-level busy handler. A competing writer can therefore hold the call inside SQLite beyond the configured patience window, preventing the jitter/deadline loop from raising the intended lock-attribution error and making sub-second activity-write budgets ineffective. The correction belongs at transaction acquisition: make only `.

### HERMES-131 — Verified bounded quick-snapshot recovery

- **Summary:** Keep a bounded recovery set (recent configured window, newest complete generation and needed verified per-database coverage) rather than an unbounded history of partial snapshots. Separate automatic snapshot families from manual snapshots. This is recovery retention, not an archive of every historical config version.
- **Source surfaces:** `hermes_cli/backup.py`, `tests/hermes_cli/test_backup.py`, `tests/hermes_cli/test_quick_retention_repro.py`. Preserve the fork's existing full-backup session exclusions.
- **September 13 review reconciliation:** PR #106101's live P1 reproduced on this fork: a different-size but healthy SQLite replacement cleared recovery obligations and allowed pruning of the usable copy although restore rejects the replacement. All per-database recovery witnesses now require manifest size agreement as well as integrity. Real-file automatic/manual pruning and legacy-checkpoint tests cover both sites. This is not content authentication for same-size replacements. Updating the contribution branch is separate from this source-only cron's fork publication; its remote parity remains unverified.
- **Upstream tracking:** [NousResearch/hermes-agent#106087](https://github.com/NousResearch/hermes-agent/issues/106087), independently reproduced missing/corrupt recovery-payload and complete-generation hardening follow-up. Related #58672 concerns global automatic keep=1 scope; closed #90613 concerns source-side corruption detection rather than existing recovery-payload validation.
- **Upstream PR:** Source proposal [#97768](https://github.com/NousResearch/hermes-agent/pull/97768), open when checked 2026-09-08. Reuses @tachyon-r's `f1b317f9dd21f192339f6fbe3951176f401e43e1` and `aaad9f482eb3c8fc077a4ca06dee61a9b01d96cb` with original authorship. Hardening follow-up [#106101](https://github.com/NousResearch/hermes-agent/pull/106101) closes #106087 and preserves that dependency explicitly.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_quick_retention_repro.py tests/hermes_cli/test_backup.py tests/hermes_cli/test_backup_stability.py tests/hermes_cli/test_backup_all_profiles.py tests/hermes_cli/test_backup_path_errors.py tests/agent/test_curator_backup.py`. Disposable homes and real SQLite databases only; sparse-file reproduction crosses the actual 1 GiB cap without allocating that payload.
- **Rollback:** Revert only this retention candidate's `backup.py` pruning/copy-metadata hunks and focused tests, including the two source-proposal commits, or revert its landed fork PR. Do not remove other full-backup exclusions or touch any existing snapshots. Rollback prevents future pruning behavior; it cannot recreate snapshots already expired under the configured retention contract.
- **Retirement:** Remove the fork-only implementation when released upstream passes bounded incomplete retention, newest complete generation, readable omitted-DB coverage, alternating failures, and snapshot-family/profile isolation regressions. Preserve useful behavior tests only when not duplicated upstream.
- **Runtime scope:** Source-only. No live snapshot cleanup, configuration change, B2 action, deployment or restart authorized or performed.
- **Additional historical subjects (optional provenance):** `fix(backup): retain verified quick-snapshot recovery generations`.
