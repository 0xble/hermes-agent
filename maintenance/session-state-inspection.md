# Typed Session-State Inspection

Load this unit when changing the typed `session_state` tool, its Telegram bundle registration, read-only session exports, or the state.db command preview boundary.

## Required behavior

The opt-in `session_state` tool exposes only three bounded read-only actions: `resolve_topic`, `inspect_session`, and `inspect_delivery`. It is profile-scoped, uses the active Hermes state path, rejects SQL-shaped selectors, caps fields and serialized output, and does not create, migrate, or write `state.db`. Under multiplexed routing, delivery inspection reads the process-home shared ledger filtered by adapter profile, while session selectors resolve in the served profile's state. Delivery content is omitted unless explicitly requested and remains capped. Session transcript messages are never returned. `session_search` remains the owner of conversational recall.

Session inspection includes boolean `archived` and, when the column exists, `auto_archived` provenance. Legacy NULL/blank session owners resolve only to the profile owning their store, while legacy NULL/blank delivery adapter owners resolve to `default`. Delivery text fields retain the existing per-field caps, and oversized row sets are reduced repeatedly before the fixed byte-cap error is returned.

The CLI exposes the same state handlers through `hermes sessions state resolve-topic --chat-id X --thread-id Y`, `hermes sessions state inspect --session-id S`, and `hermes sessions state delivery` with exactly one of `--session-id`, `--session-key`, or `--obligation-id` and optional `--include-content`. CLI state reads use the canonical owner of the home selected by `-p` or `HERMES_HOME`, even when a parent turn supplied a different `HERMES_SESSION_PROFILE`. Native tool calls retain their bound session profile. Real CLI subprocess regressions cover this intentional distinction across state actions and exclude foreign-owned session rows. It prints the tool JSON, exits 0 for successful/not-found reads, and exits nonzero for errors. `hermes sessions search QUERY [--limit N] [--after X] [--before Y]` reuses the existing `session_search` discovery handler against a read-only current-profile store. Neither CLI route needs its toolset enabled or accepts SQL.

Ordinary `hermes sessions export` opens the selected profile's store with `SessionDB(read_only=True)` for every format. It must not create or migrate the source database. A missing store reports no sessions without creating a database or export, and a legacy schema reports the migration requirement without changing the store. Only an exact `--session-id` export in `md` or `qmd` format with both `--delete-after-verified` and `--yes` opens a writer for intentional verified deletion. An unconfirmed deletion request remains read-only and is refused before export.

Raw commands that target Hermes `state.db` are neutralized in progress previews without hiding unrelated SQLite commands.

## Provenance and patches

Fork patch identity: `session-state-inspection`.

The tool is registered in its own opt-in toolset and folded into the Telegram bundle, not the core toolset. Source surfaces are `tools/session_state_tool.py`, `toolsets.py`, `agent/display.py`, `tests/tools/test_session_state_tool.py`, `tests/agent/test_display.py`, and the shared-metrics tool/schema contract.

The observational CLI foundation comes from [upstream PR #111627](https://github.com/NousResearch/hermes-agent/pull/111627), merged at [30a299180c79b357187e60c21973b250aaf2a96d](https://github.com/NousResearch/hermes-agent/commit/30a299180c79b357187e60c21973b250aaf2a96d), with head `a652cfbf14e295d1fd10fb0caf3d46b5df719289`. It preserves KoNit-K's original work from #110186, includes follow-ups by kshitijk4poor, and credits Ahmett101 as co-author. Its session dispatch makes `list`, `stats`, and `pinned` read-only, handles missing stores and migration errors, but does not classify exports as observational. The fork extends that dispatch to non-deleting exports in [8027f53431a9394881b8e2194f1f012fe9c9d10f](https://github.com/0xble/hermes-agent/commit/8027f53431a9394881b8e2194f1f012fe9c9d10f), with real CLI coverage in `tests/hermes_cli/test_sessions_export_read_only.py`. That commit's malformed upstream token is not a valid provenance reference. This maintained record supersedes that citation without changing the historical commit.

## Verification

Run `HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/tools/test_session_state_tool.py tests/agent/test_display.py tests/hermes_cli/test_sessions_inspect.py tests/hermes_cli/test_sessions_export_read_only.py tests/tools/test_session_search.py tests/ci/test_prompt_surface.py` and `scripts/run_tests.sh tests/hermes_cli/test_shared_metrics_efficiency.py`. The focused proof must cover real registry dispatch against temporary profiles, named-profile resolution without a bound session profile, multiplexed delivery reads from the process-home ledger, identical-looking session ids without cross-profile fallback, an absent state database without creation, topic and delivery reads, bounded output, SQL-shaped rejection, no migration during reads, and preview redaction. CLI proof compares the parsed commands' JSON with the exact tool handlers, including discovery limits/time bounds, current-store isolation, not-found success, and nonzero errors. Export proof uses real CLI subprocesses and disposable stores to cover every format, selected-profile and read-only stores, no creation or migration, refusal without confirmation, and intentional verified deletion in both `md` and `qmd`.

## Retirement and rollback

Retire when a released upstream Hermes surface provides an equivalent typed, bounded, profile-scoped read-only route and preview boundary while preserving the same no-migration and no-transcript guarantees. Roll back by reverting the commits carrying `Fork-Patch: session-state-inspection`; no persisted data or schema migration is part of this patch.
