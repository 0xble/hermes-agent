# Credential isolation and inference routing

This responsibility covers pooled accounts, effective inference routes, OAuth refresh and retry/fallback selection. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

## Coupled current adaptations

- Codex image generation snapshots explicit gateway endpoint, key and API mode together once per generation, preserving that transport through input preparation and nonfinal-result retries. Direct Codex keeps Responses and never borrows gateway credentials. Regression: `tests/plugins/image_gen/test_codex_transport_snapshot.py` drives the real provider/config loader through HTTPX with generation/edit and configuration-rotation cases. The existing gateway feature comes from fork PR `#136`; upstream `#91131` and `#65323` remain related open proposals as checked on 2026-09-15, not equivalent fixes or contribution associations.

- Runtime credential refresh may recover a missing dispatched ID only through one uniquely matching request-key hint. Missing CLI targets without a hint, unknown hints and ambiguous matches still refresh no account. This extends HERMES-121 without weakening its explicit CLI targeting contract. Regression: `tests/agent/test_credential_pool_operations.py`.

- Preset-reference restoration strips unauthored empty fallback chains.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-021 | Active |
| HERMES-023 | Active |
| HERMES-024 | Active |
| HERMES-104 | Active |
| HERMES-106 | Active |
| HERMES-116 | Active |
| HERMES-117 | Active |
| HERMES-118 | Active |
| HERMES-119 | Active |
| HERMES-120 | Active |
| HERMES-121 | Active |
| HERMES-129 | Active |

## Patch records

### HERMES-021 — Try alternate same-provider credentials before provider fallback

- **Summary:** Adds a bounded, provider-neutral alternate-credential attempt in the credential-pool recovery path. A timeout, server error, or provider overload may swap to one different healthy entry from the active provider's matching pool without marking the failing credential exhausted; existing rate-limit and billing paths continue to exhaust and rotate credentials, while authentication failures and upstream-aggregator 429s retain their existing refresh/fallback behavior. A 60-second process-local, profile-scoped soft cooldown steers nearby pool instances away from the transiently failing credential without making the pool unavailable, and structured rotation/outcome logs correlate the serving entry with latency and provider-reported prompt-cache usage while turn-boundary and interruption cleanup prevents stale outcome attribution. This prevents a transient provider-edge failure from jumping directly to a fallback model or immediately routing nearby calls back to the same overloaded credential while another compatible same-provider credential remains healthy.
- **Surfaces:** `agent/agent_runtime_helpers.py`; `agent/conversation_loop.py`; `agent/credential_pool.py`; `agent/turn_retry_state.py`; `run_agent.py`; `tests/agent/test_credential_pool_routing.py`; `tests/agent/test_turn_retry_state.py`; `tests/agent/test_run_agent.py`.
- **Upstream tracking:** Issue `#22916` asks for same-provider profile rotation before provider fallback but remains open without implementation. PRs `#24539` and `#11034` instead eagerly activate fallback for overloaded providers; PR `#84128` extends same-account Codex backoff. None provides one alternate same-provider account attempt before provider fallback.
- **Upstream PR:** None directly implements the patch after checked 2026-08-14. Related: #24539, #11034, and #84128.
- **Regression:** `scripts/run_tests.sh tests/agent/test_credential_pool_routing.py`.
- **Rollback:** Remove the alternate-entry selector, transient soft-cooldown registry, rotation/cache telemetry, and overload/transport branch from `recover_with_credential_pool`, then remove only the HERMES-021 focused tests. Preserve rate-limit retry semantics, billing rotation, auth refresh behavior, prompt-cache key construction, and upstream-aggregator fallback bypass.
- **Retirement:** Retire after a released upstream implementation provides capability-based same-provider credential alternation before provider fallback for transient overload, server-error, and timeout failures and passes equivalent focused tests.
- **Additional historical subjects (optional provenance):** `fix(agent): try alternate credential before provider fallback`; `fix(agent): soften transient credential overloads`; `fix(agent): clear stale rotation telemetry`; `fix(agent): generalize transient alternate credential recovery`.

### HERMES-023 — Route auxiliary provider overload through fallback

- **Summary:** Uses the shared API error classifier to recognize provider-overload responses, including status-less overload messages, as auxiliary capacity failures. Sync and async auxiliary calls now continue through the configured model/provider fallback chain after overload instead of aborting compression or another side task.
- **Surfaces:** `agent/auxiliary_client.py`; `tests/agent/test_auxiliary_client.py`.
- **Upstream tracking:** Local fork behavior; upstream equivalence has not yet been established.
- **Upstream PR:** None.
- **Regression:** `pytest -q tests/agent/test_auxiliary_client.py -k 'AuxiliaryOverloadFallback'`.
- **Rollback:** Remove `_is_overload_error`, its sync/async fallback predicates and reason labels, and the HERMES-023 focused tests. Preserve all existing auth, billing, connection, rate-limit, model-compatibility, and response-validation fallback behavior.
- **Retirement:** Retire after released upstream routes classified provider overload through equivalent sync and async auxiliary fallback chains.
- **Additional historical subjects (optional provenance):** `fix(auxiliary): route provider overload through fallback chain`.

### HERMES-024 — Retry auxiliary transient failure on one alternate credential

- **Summary:** For classified provider overload, server-error, and timeout failures, sync and async auxiliary calls select one distinct healthy runtime credential from the matching provider pool only when the failed runtime credential is exactly attributable. The retry binds that credential to an isolated auxiliary client, leaves the main conversation route and pool cursor unchanged, applies only a short soft cooldown to the failed entry, and never marks either credential exhausted for a transient failure.
- **Surfaces:** `agent/auxiliary_client.py`; `tests/agent/test_auxiliary_client.py`.
- **Upstream tracking:** Local fork behavior; upstream equivalence has not yet been established.
- **Upstream PR:** None.
- **Regression:** `pytest -q tests/agent/test_auxiliary_client.py -k 'AuxiliaryTransientCredentialRetry or AuxiliaryOverloadFallback'`.
- **Rollback:** Remove `_transient_credential_retry_reason`, `_select_transient_aux_alternate`, the sync/async alternate retry blocks, and the HERMES-024 focused tests. Preserve HERMES-023 overload fallback and all durable auth, billing, and rate-limit rotation behavior.
- **Retirement:** Retire after released upstream provides provider-neutral, non-exhausting, bounded same-provider credential alternation for equivalent auxiliary transient failures before model/provider fallback.
- **Additional historical subjects (optional provenance):** `fix(auxiliary): retry transient failure on one alternate credential`; `fix(auxiliary): require exact failed credential identity`; `fix(auxiliary): bind and preserve alternate credential recovery`.

### HERMES-104 — Isolate manually added Codex accounts

- **Summary:** Restrict singleton auth-store resynchronization to the canonical `device_code` seed. Manual device-code entries retain their own access and refresh tokens.
- **Surfaces:** `agent/credential_pool.py`; `tests/agent/test_credential_pool.py`; this record.
- **Upstream tracking:** Fork-origin correction accepted on Brian's owned remote as #46; not present in released canonical upstream as of 2026-09-03.
- **Upstream PR:** None.
- **Regression:** `scripts/run_tests.sh tests/agent/test_credential_pool.py -q`; the manual-entry case must preserve both tokens and avoid persistence while singleton seed behavior remains covered by the existing suite.
- **Expected published commit identity:** Stable subject `fix(auth): isolate manually added Codex accounts (#46)`; source, regression, and this record ship together.
- **Rollback:** Revert the stable subject only if manual Codex entries are removed or receive a separate account-bound resynchronization source. No persistent schema changes are involved.
- **Retirement:** Retire after released upstream distinguishes singleton-seeded and manual Codex accounts at the auth-store synchronization boundary and passes equivalent multi-account regression coverage.

### HERMES-106 — Rotate credentials across transient retries

- **Summary:** Track attempted logical account identities across one retry sequence and select one previously untried eligible credential before each transient same-provider retry. Preserve priority, reservation, cooldown, durable quota and auth handling, hard retry ceilings, transport recovery, provider-wide overload behavior, and configured cross-provider fallback.
- **Surfaces:** `agent/agent_runtime_helpers.py`; `agent/conversation_loop.py`; `agent/credential_pool.py`; `agent/turn_retry_state.py`; `run_agent.py`; focused credential-pool and retry regressions; this record.
- **Upstream tracking:** Not yet filed upstream as of 2026-09-02. The maintained-fork PR is the only published implementation currently tracked.
- **Upstream PR:** None as of 2026-09-02.
- **Regression:** `scripts/run_tests.sh tests/agent/test_credential_pool_routing.py tests/agent/test_turn_retry_state.py tests/agent/test_32646_fallback_429_after_timeout.py -q`; coverage must prove priority order, duplicate logical-account suppression, exhausted and cooled credential handling, no-alternate behavior, retry ceilings, provider-wide and Z.AI overload handling, transport recovery, and final configured fallback.
- **Published commit identity:** Stable subject `fix(agent): rotate credentials across transient retries`; reconciled by `fix(maintenance): reconcile concurrent origin baseline` after the maintained branch was rebased.
- **Rollback:** Revert the credential-rotation hunks from the reconciliation commit, removing attempted-account retry state, alternate-credential selection, focused regressions, the index row, and this record. No persistent-data migration rollback is required.
- **Retirement:** Retire after released upstream Hermes provides equivalent logical-account rotation within bounded transient retries while preserving the listed failure classes and passes equivalent focused regressions.

### HERMES-116 — Reset one pooled credential by target

- **Summary:** `hermes auth reset <provider> [target]` accepts an optional index, entry id, or exact label resolved through the same `resolve_target()` as `auth remove`, and clears only that entry through a new `CredentialPool.reset_status(credential_id)`, which persists with `status_cleared_ids` so the disk-recency merge cannot copy the still-binding cooldown back. The pool-wide form is unchanged. Motivated by the 2026-09-06 incident where un-benching one recovered Codex account required either the pool-wide reset (which retries every exhausted sibling first under `fill_first`) or hand-editing `auth.json`.
- **Surfaces:** `agent/credential_pool.py` (`_cleared_status_copy`, `reset_status`), `hermes_cli/auth_commands.py`, `hermes_cli/subcommands/auth.py`, `tests/hermes_cli/test_auth_commands.py`, `tests/agent/test_credential_pool.py`, `website/docs/user-guide/features/credential-pools.md`, `website/docs/reference/cli-commands.md`, and this manifest.
- **Upstream tracking:** [PR #104910](https://github.com/NousResearch/hermes-agent/pull/104910), merged as `134b173efab4038023274cbc56d20140bc8b6c49`, supplies targeted reset and stale-write-safe persistence. Source is replaced, but this fork record remains Active pending runtime acceptance.
- **Upstream PR:** #104910 merged; no runtime retirement until accepted separately.
- **Regression:** `scripts/run_tests.sh tests/agent/test_credential_pool_operations.py` covers targeted reset persistence, untouched sibling cooldowns, missing IDs, and pool-wide reset.
- **Published commit identity:** Stable subject `feat(auth): reset one pooled credential by target`.
- **Rollback:** Revert only that stable-subject commit. It touches no credentials, schema, or persisted state; reverting restores the pool-wide-only `reset`.
- **Retirement:** Retire when a released upstream version accepts a per-credential target on `hermes auth reset` and clears only that entry with the cooldown surviving persistence, with these regressions passing against it.

### HERMES-117 — Add hermes auth refresh for pooled OAuth credentials

- **Summary:** New `hermes auth refresh <provider> [target]` resolves one pooled credential (target optional when the pool holds exactly one), refuses `api_key` entries and providers outside `REFRESHABLE_OAUTH_PROVIDERS` (anthropic, nous, openai-codex, xai-oauth, the set `_refresh_entry_impl` actually rotates), and calls `try_refresh_matching(credential_id=...)`. Success rotates the tokens and clears the local exhaustion block through the pool's own `_MARK_OK` (proving the grant is alive, not that quota is back: a still-capped account 429s on its next request); failure leaves the pool's verdict in place and reports it. `_refresh_entry_impl` now persists that success with `status_cleared_ids`, because a borrowed row has no access_token on disk and a plain persist copied the cooldown back. This is the supported form of the 2026-09-06 recovery: a Codex account reset with a banked credit stayed frozen behind a stale `last_error_reset_at` because the quota probe used its expired access token, and a targeted forced refresh returned it to rotation immediately.
- **Surfaces:** `agent/credential_pool.py` (`REFRESHABLE_OAUTH_PROVIDERS`), `hermes_cli/auth_commands.py` (`auth_refresh_command`), `hermes_cli/subcommands/auth.py`, `tests/hermes_cli/test_auth_commands.py`, `website/docs/user-guide/features/credential-pools.md`, `website/docs/reference/cli-commands.md`, and this manifest.
- **Upstream tracking:** [PR #104910](https://github.com/NousResearch/hermes-agent/pull/104910), merged as `134b173efab4038023274cbc56d20140bc8b6c49`, supplies the native locked per-credential refresh retained by HERMES-121 verification. Source is replaced, but this fork record remains Active pending runtime acceptance.
- **Upstream PR:** #104910 merged; no runtime retirement until accepted separately.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_auth_pool_operations.py tests/hermes_cli/test_named_account_quota_cli.py` covers exact-target real HTTP refresh success, transient and terminal failures, sibling preservation, persisted cleared cooldowns, ambiguous and unsupported targeting, and verified-refresh readback.
- **Published commit identity:** Stable subject `feat(auth): add hermes auth refresh for pooled OAuth credentials`.
- **Rollback:** Revert only that stable-subject commit. It adds a command and a constant; nothing in the runtime selection path changes, and reverting removes the command only.
- **Retirement:** Retire when a released upstream version ships an equivalent per-credential refresh command (or an automatic refresh-before-probe that makes it unnecessary, per #89415) with these regressions passing against it.

### HERMES-118 — Show entry id and priority in auth list

- **Summary:** `hermes auth list` prints `id=<entry id>` and `priority=<n>` on every row. `auth remove`/`auth reset`/`auth priority` accept an entry id and `resolve_target()` tells the user to use one on ambiguous labels, but nothing printed ids; `priority` decides `fill_first` order and was invisible.
- **Surfaces:** `hermes_cli/auth_commands.py` (`auth_list_command`), `tests/hermes_cli/test_auth_commands.py`, `website/docs/user-guide/features/credential-pools.md`, and this manifest.
- **Upstream tracking:** [PR #104874](https://github.com/NousResearch/hermes-agent/pull/104874), merged as `af212103b0a890041b758183997647a643c09138`, supplies credential identity and priority display. Source is replaced, but this fork record remains Active pending runtime acceptance.
- **Upstream PR:** #104874 merged; no runtime retirement until accepted separately.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_auth_commands.py` covers entry ID and priority columns.
- **Published commit identity:** Stable subject `feat(auth): show entry id and priority in auth list`.
- **Rollback:** Revert only that stable-subject commit. Output-only change.
- **Retirement:** Retire when a released upstream `hermes auth list` shows entry id and priority.

### HERMES-119 — Count selections under every strategy

- **Summary:** `CredentialPool._select_unlocked()` bumps the chosen entry's `request_count` under every strategy, before the `round_robin` rotation persists so the bump is in that snapshot. Previously only the `least_used` branch counted, so the counter stayed at a permanent 0 under every other strategy (the 2026-09-06 Codex pool showed 0 on every entry after 3,681 calls in five hours) and switching a pool to `least_used` started from a fake all-zero baseline. The bump stays in memory and reaches `auth.json` on the next persist, as it always did under `least_used`; the forced-refresh target lookup passes `count=False`. Selection order is unchanged under all four strategies.
- **Surfaces:** `agent/credential_pool.py` (`_select_unlocked`), `tests/agent/test_credential_pool.py`, and this manifest.
- **Upstream tracking:** [PR #104910](https://github.com/NousResearch/hermes-agent/pull/104910), merged as `134b173efab4038023274cbc56d20140bc8b6c49`, counts the returned entry under each strategy. Source is replaced, but this fork record remains Active pending runtime acceptance.
- **Upstream PR:** #104910 merged; no runtime retirement until accepted separately.
- **Regression:** `scripts/run_tests.sh tests/agent/test_credential_pool_operations.py` covers returned-selection counting across all four strategies, persistence, and non-counting peek/refresh lookup.
- **Published commit identity:** Stable subject `fix(credential-pool): count selections under every strategy`.
- **Rollback:** Revert only that stable-subject commit. Counters already persisted stay in `auth.json` and are harmless; reverting restores the least_used-only bump.
- **Retirement:** Retire when a released upstream version counts selections under every strategy with equivalent regressions passing.

### HERMES-120 — Place pooled credentials by priority

- **Summary:** `CredentialPool.move_entry(credential_id, priority)` places one entry at a raw 0-based priority, keeps priorities a contiguous `0..n-1` sequence as `remove_index()` does, clamps out-of-range values, and persists. `hermes auth add <provider> --priority N` moves the credential returned by the upstream add path (with a non-fatal note when its pool identity is unavailable) and `hermes auth priority <provider> <target> <N>` moves an existing one by index, id, or exact label. `move_entry` applies `_normalize_pool_priorities` up front so the persisted order is the one the next load produces, and both commands report the effective priority with a stderr note explaining a difference (clamped to the pool size, or `anthropic` keeping manually added credentials ahead of seeded ones) and another when the provider's strategy is not `fill_first` (`round_robin` rewrites priorities on each selection; `random`/`least_used` ignore them). Placement after `add` never fails the command once the credential is saved. Requested on 2026-09-06 so a freshly added Codex account can be drawn before a nearly exhausted one without editing `auth.json`.
- **Surfaces:** `agent/credential_pool.py` (`move_entry`), `hermes_cli/auth_commands.py` (`auth_add_command`, `_add_credential`, `_report_priority`, `auth_priority_command`), `hermes_cli/subcommands/auth.py`, `tests/agent/test_credential_pool.py`, `tests/hermes_cli/test_auth_commands.py`, `website/docs/user-guide/features/credential-pools.md`, `website/docs/reference/cli-commands.md`, and this manifest.
- **Upstream tracking:** [PR #104910](https://github.com/NousResearch/hermes-agent/pull/104910), merged as `134b173efab4038023274cbc56d20140bc8b6c49`, supplies positional priority. Source is replaced, but this fork record remains Active pending runtime acceptance.
- **Upstream PR:** #104910 merged; no runtime retirement until accepted separately.
- **Regression:** `scripts/run_tests.sh tests/agent/test_credential_pool_operations.py tests/hermes_cli/test_auth_pool_operations.py` covers persisted reorder, contiguous priorities, clamping, unknown IDs, Anthropic manual-first normalization, reauthenticated-row placement, and a successfully saved add with no returned pool identity.
- **Published commit identity:** Stable subject `feat(auth): place pooled credentials by priority`.
- **Rollback:** Revert only that stable-subject commit. Priorities already rewritten in `auth.json` remain valid contiguous values; reverting restores append-only adds.
- **Retirement:** Retire when a released upstream version lets a credential be placed at a chosen `fill_first` position at add time and afterwards, with these regressions passing against it.
- **Additional historical subjects (optional provenance):** `hermes auth add --priority N`; `CredentialPool.move_entry()`.

### HERMES-121 — Inspect and verify named credential quota

- **Rationale and contract:** `hermes auth status <provider> [target] --live --json` reads exactly one stored credential without seeding, healing, persisting, selecting, or refreshing. Omitted targets require exactly one row. Cached status and timestamps are separate from live evidence. `auth refresh <provider> [target] --verify --json` uses the upstream native locked refresh, then compares the exact persisted ID and token pair before probing. Successful completion may adopt a peer rotation, not necessarily issue a POST. A successful quota probe is not an inference test.
- **Quota scope and evidence:** Live quota initially supports Codex's official endpoint only. Other providers return structured `unsupported`, never select an alternative account. Any depleted window or explicit blocking flag means exhausted. Missing, malformed, or non-finite quota never proves availability. Window kind follows provider duration, not primary/secondary position. Access 401/403 recommends refresh, not reauth. Only this native refresh attempt's terminal recovery verdict can establish `reauth_required`; cached errors and a `None` result cannot.
- **CLI contract:** `--json` produces one secret-free schema-versioned JSON object, including without `--live`/`--verify`. Exit 0 is a completed inspection (including known exhaustion) or completed refresh, 1 is unknown/unavailable/failed verification, and 2 is invalid targeting or unsupported refresh. Cached inspection works without network. Generic legacy commands without the new flags remain unchanged.
- **Surfaces:** `hermes_cli/auth_quota.py`, `hermes_cli/auth_commands.py`, `hermes_cli/subcommands/auth.py`, `agent/credential_pool.py`, `tests/hermes_cli/test_named_account_quota_cli.py`, `website/docs/reference/cli-commands.md`, and this manifest. No runtime deployment, automatic refresh, or provider-wide cooldown reset.
- **Upstream tracking:** Checked 2026-09-07. Upstream replacements are merged: [PR #104874](https://github.com/NousResearch/hermes-agent/pull/104874), commit `af212103b0a890041b758183997647a643c09138`, supplies credential identity and priority display; [PR #104910](https://github.com/NousResearch/hermes-agent/pull/104910), commit `134b173efab4038023274cbc56d20140bc8b6c49`, supplies targeted reset/refresh, selection counting, priority placement, and the stale-write-safe `reset_statuses()` persistence path. This source replacement remains pending runtime acceptance; do not call it runtime retirement. [PR #93282](https://github.com/NousResearch/hermes-agent/pull/93282) is closed unmerged; [PR #72690](https://github.com/NousResearch/hermes-agent/pull/72690) and [PR #68224](https://github.com/NousResearch/hermes-agent/pull/68224) remain open; [PR #69494](https://github.com/NousResearch/hermes-agent/pull/69494), commit `fea838c9f26ac00c5931a4ac13764ae49361428b`, is a quota-restored foundation, not proof of CLI equivalence.
- **Upstream PR:** No exact upstream PR for this combined named-account/explicit-verification contract. Track the proposals above at each maintenance sync and compare current code/tests, not just titles or merge state.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_named_account_quota_cli.py tests/hermes_cli/test_auth_commands.py tests/agent/test_credential_pool.py tests/agent/test_credential_pool_oauth_writethrough.py tests/agent/test_credential_pool_profile_oauth_fork.py`. Synthetic provider fixtures exercise stale-history separation, byte-preserving inspection, missing/ambiguous IDs, invalid payloads, weekly-only windows, independent exhaustion, no-token no-fallback, native rotation/readback with sibling isolation, and terminal versus unproven failure. Real CLI JSON smoke uses isolated fixtures; a real-account read-only smoke does not rotate credentials.
- **Rollback:** Revert stable subject `feat(auth): inspect and verify named credential quota` while retaining upstream targeted refresh. Remove new CLI flags/targeted status route, quota module, read-only loader, and ephemeral native refresh evidence together.
- **Retirement:** Retire after released upstream offers equivalent exact-target read-only inspection plus native locked refresh, persisted readback, structured outcomes, safe JSON, and regression coverage. Keep any still-missing contract independently until verified.

### HERMES-129: Explicit session model controls

- **Upstream tracking:** https://github.com/NousResearch/hermes-agent/issues/16525. Prior implementation PRs 43145 and 43153 were closed unmerged at inspection. Retire this patch when a released native agent-callable operation satisfies the contract.
- **Upstream PR:** None submitted for this implementation. Closed predecessors: https://github.com/NousResearch/hermes-agent/pull/43145 and https://github.com/NousResearch/hermes-agent/pull/43153.
- **Contract:** One optional `session_model` plugin tool changes model/provider/reasoning only on explicit user direction, with session ownership enforced by the runtime and intent constrained by tool instructions. Combined validation, end-of-turn application, native context/provider handling, rollback, session persistence, and truthful queued/applied/failed receipts. No autonomous routing or global changes.
- **Source:** `plugins/session-model/`, `hermes_cli/session_model.py`, frontend adapters in `gateway/session_model.py` and `hermes_cli/session_model_cli.py`, the additive PluginContext method, and bounded CLI/gateway turn bindings.
- **Verification:** `scripts/run_tests.sh -j 2 tests/plugins/test_session_model.py tests/plugins/test_session_model_transport.py`, plus native model persistence, context-switch and inference-control regressions. The transport fixture uses real agent imports and HTTP requests, not paid inference. Runtime promotion is separate.
- **Rollback:** Revert the commit titled `feat(session): add explicit model and reasoning tool`, including its focused tests and this entry. If later changes share the turn runners or PluginContext, remove only the session-model binding and API after confirming no remaining consumers. Preserve native slash controls.
- **Retirement:** Remove the plugin, its API/bindings, and duplicate tests when released upstream satisfies the same regressions. Remove enabled-plugin configuration during separately authorized runtime promotion.
- **Additional historical subjects (optional provenance):** `merge: reconcile session model metadata before controlled QA`; `fix(session): declare model control tool in plugin metadata`.
