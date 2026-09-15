# Cron scheduling, route selection and outbound authority

This responsibility covers cron job/fleet configuration, schedule/claim identity, profile-bound sends and completion verifiers. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

## Coupled current adaptations

Deferred cron processing returns the actual finalizer result. A replaced fire
owner, foreign execution owner or missing execution ledger cannot be reported as
successfully deferred. The special path still avoids ordinary completion
accounting and preserves foreign state. Real script/job/ledger races in
`tests/cron/test_contention_deferral.py` verify these refusals.

Paused recurring manual fires also retain their claimed schedule snapshot on
deferral, including an explicit null next-run time. The cooldown stays in the
durable deferred record and its execution settles without ordinary run accounting.
The job stays paused: after cooldown, another explicit manual fire can retry while
preserving the schedule. Explicit resume instead supersedes deferred work and
computes a fresh future schedule, following the existing lifecycle-edit contract.
The real script/scheduler/job/ledger regression in
`tests/cron/test_contention_deferral.py` covers interval and wall-clock schedules,
both retry routes, and unchanged paused markers. This is a fork-only adaptation:
the deferral module is absent from frozen upstream `743140cd8221ad04897c4c3e706e0fc8a7613b8e`.
Retire this guard only when released upstream satisfies the same pause and ledger
contract. Roll back this guard and its focused regression together, without
rewriting live schedules or deferred execution records.

Cron transport selection uses the gateway's launch-time primary identity and
the selected adapter's resolved creation home. Per-turn identity alone can select
the wrong primary bot, and the name `custom` is shared by unrelated homes. Missing
or mismatched adapter-home evidence refuses delivery before dispatch. The common
adapter factory stamps every replacement instance. Focused coverage includes
real scoped factory construction, secondary ownership, custom-home collisions,
and retry-safe refusal. Shared corrections are published to existing contribution
PR #86648 at `a46133cd0e8d327813ca39f7359c2cfd2d79435b`, with a clean continuation
of its configured Hermes review. The exact contribution head was read back on September 14 and reported no hosted checks. This supersedes earlier unverified contribution-parity notes only for the named correction, not all historical differences from that PR.

Later review corrections preserve elapsed cron deferral time across daylight-saving
transitions by adding cooldowns in UTC, then serializing in the profile timezone.
Direct Python `cronjob` callers retain upstream's positional prefix through `paused_reason`.
The fork's `timezone` and `allow_messaging` options are keyword-only, preventing a
historical positional task ID from becoming messaging permission. Callers that used
those fork options positionally must use their names. This signature drift predates
the current sync. Real scheduler and persisted job-store regressions
cover these corrections without changing live jobs or runtime configuration.

## Named cron route follow-up (2026-09-12, active)

- **Stable subject:** `feat(cron): follow named model presets at fire time`.
- **Origin:** Brian requested a real preset reference for `maintain-targets`; copied provider/model/reasoning pins do not follow later preset edits. Extends the static model-preset work tracked at upstream #107745 / PR #107760 (still open at audit time; upstream main inspected at `1de9963ce7617bec938572026ebc8613a9cd2f84`).
- **Preserve:** job/fleet references remain authored names; runtime-only expansion precedes preflight/provider/agent construction. Explicit inline job axes keep precedence over fleet defaults; named job routes beat creation snapshots. Preset reasoning and authoritative fallback chains propagate without promoting route reasoning into a sticky per-job pin. Literal model IDs, user-owned inference selection, profile isolation, unrelated cron fields and raw config writers remain unchanged. TUI runtime and gateway hygiene reads use the same preset resolver; model-change diagnostics exclude snapshots bypassed by a job/fleet reference.
- **Verify:** `scripts/run_tests.sh -j 4 tests/cron/ tests/hermes_cli/test_model_presets.py tests/hermes_cli/test_config.py tests/gateway/test_model_preset_runtime.py tests/tools/test_cronjob_tools.py`; focused new temp-home coverage is `tests/cron/test_cron_model_presets.py`. Web `npm run test -- src/lib/cron-job.test.ts` and `npm run typecheck` cover authored-name/clear payloads. Agent inference is doubled only in the full scheduler construction test; provider/config/store resolution is real and no maintenance workload is fired.
- **Structured-route audit:** API `model_routes` and platform `channel_overrides` now accept explicit references through the same static resolver and preserve names on config saves. Their startup-loaded route bundles reach fresh and cached agents; existing session/request precedence and credential ownership remain. CLI/Kanban/env literal invocation pins deliberately remain literal; omitted pins inherit the profile main preset. `tests/gateway/test_model_preset_routes.py` covers actual typed config/provider/agent boundaries, missing-credential refusal, and precedence.
- **Review/retire:** independent-context review found fleet-prefix provider hijacking, legacy fallback leakage and literal-tag ambiguity. Removed cron prefix reinterpretation entirely; named references provide reuse without changing literal grammar. Authoritative cron chains now remove the runtime-only legacy fallback source. Retire this extension when released upstream preserves these scheduler, roundtrip and authority regressions; an open preset PR alone is not retirement evidence.
- **Rollback/activation:** return live references to explicit pins before installing older source. Source landing does not activate a gateway; use the managed updater after informed restart approval. No cross-profile migration is implied.

- Native standalone Telegram text delivery retains the last confirmed message receipt and accepted chunk count if a later chunk fails. It stops without replaying accepted prefixes; a first-chunk failure remains ambiguous. `tests/cron/test_cron_outbound_messages.py` exercises the actual native helper with a scoped profile and fake Bot transport. The interrupted-worker authorization fixture likewise carries its real frozen route while preserving every worker-lease and stale-receipt fence.

- Adopt upstream cron wall-clock/DST calculations and elapsed-time intervals while retaining per-job IANA zones, reservation ownership, paused manual-run schedules, and bounded unreachable-model retries.

- Keep the fork's inline cron schedule/timezone recompute and run-scoped fire claim, and carry upstream's `pending_slot` invalidation on both explicit lifecycle rewrite and occurrence claim.

- A cron timezone move invalidates an unclaimed `pending_slot`.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-013 | Active |
| HERMES-014 | Active |
| HERMES-015 | Active |
| HERMES-022 | Active |
| HERMES-036 | Active |
| HERMES-057 | Retired |

## Patch records

### HERMES-013 — Pin cron wall-clock schedules to per-job IANA timezones

- **Legacy timestamp contract:** Offset-free persisted values retain upstream `_ensure_aware` system-local interpretation before conversion to the effective job zone. Do not attach the configured zone to old naive values. `tests/test_timezone.py` exercises actual due-job selection across differing zones.

- **Summary:** Adds an optional validated `timezone` field to cron jobs across persistence, scheduler calculations, tool/API/CLI/web/desktop surfaces, including update/clear recalculation, DST behavior, restart persistence, and legacy profile-timezone inheritance. Intervals and absolute one-shots retain their original semantics.
- **Surfaces:** `cron/jobs.py`; `tools/cronjob_tools.py`; gateway/CLI/web/desktop cron surfaces; `tests/cron/test_job_timezone.py`; `tests/cron/test_cron_timezone_migration_catchup.py`; associated cron UI/API tests.
- **Upstream tracking:** Issue `#26549`; open PR `#27393` superseded `#21926` but was incomplete for this explicit job-field contract when the patch was implemented.
- **Upstream PR:** Associated: #27393 (open) and superseded #21926 (closed; checked 2026-08-14).
- **Regression:** Run `scripts/run_tests.sh` over `tests/cron/test_job_timezone.py`, `tests/cron/test_cron_timezone_migration_catchup.py`, and the associated cron schema, script, gateway API, and CLI files; run the web and desktop cron model tests with their repository commands. Include the explicit-timezone classifier case so a legacy UTC row is checked in its own wall clock before conversion to the pinned zone.
- **Rollback:** Inventory every persisted job with an explicit timezone. Migrate each to the released upstream representation or an equivalent profile/schedule arrangement before removing the private field. Then revert the stable-subject commit in a follow-up change, resolve current upstream overlap, remove duplicate UI/API/tool fields and fork-only tests, and prove New York/Los Angeles separation, profile fallback, update/clear behavior, DST, restart persistence, and interval/one-shot invariance against upstream.
- **Additional historical subjects (optional provenance):** `feat(cron): support per-job timezones`; `fix(reconcile): preserve cron and doctor contracts`; `fix(reconcile): preserve cwd and timezone contracts`.

### HERMES-014 — Cron CLI failure propagation

- **Summary:** Returns `cron_command(args)` from `cmd_cron` so nonzero cron subcommand results reach the top-level dispatcher and process exit status instead of being discarded as `None`.
- **Surfaces:** `hermes_cli/main.py`; `tests/hermes_cli/test_cron.py`.
- **Upstream tracking:** Current `upstream/main` still calls `cron_command(args)` without returning its result. Retire when a released upstream dispatcher propagates cron failures through an equivalent process-status contract.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `source venv/bin/activate && python -m pytest -q tests/hermes_cli/test_cron.py -k top_level_handler_propagates_failure_status`.
- **Rollback:** Once the released upstream dispatcher owns the same exit-status contract, remove the private `return` change and delete only `test_top_level_handler_propagates_failure_status` if upstream provides equivalent coverage. Run `tests/hermes_cli/test_cron.py`, invoke a deliberately failing read-only cron CLI operation, and verify its nonzero process status before promotion.
- **Additional historical subjects (optional provenance):** `fix(cron): propagate CLI failures`.

### HERMES-015 — Isolate gateway and cron workdir state

- **Summary:** Captures the gateway's configured cwd before cron execution begins and binds cwd into each gateway or cron execution through the existing session ContextVar. Cron no longer mutates process-global `TERMINAL_CWD`, takes a global readers/writer lock, or serializes workdir jobs. Prompt/context discovery, terminal, file reads, project-mode code execution, and delegation resolve against each job's workspace while unbound CLI contexts retain `TERMINAL_CWD` as their baseline.
- **Narrowed 2026-08-31:** released upstream now owns the readers/writer lock removal, the sequential workdir pool removal, and `TERMINAL_CWD` non-mutation, replacing them with task-scoped binding (`record_session_cwd`/`clear_session_cwd`). Fork retains only the task-resolved read path in `tools/file_tools.py` (a shared backend's mutable cwd must not read another session's file) and the `agent/runtime_cwd.py` layer consumed by ten modules. The rollback route that spoke of restoring the lock and sequential pool is unreachable and no longer applies.
- **Surfaces:** `agent/runtime_cwd.py`; `agent/prompt_builder.py`; `gateway/run.py`; gateway session surfaces; `cron/scheduler.py`; `tools/file_tools.py`; cwd consumers in agent/tool modules; gateway and cron workdir regressions.
- **Upstream tracking:** Issue `#81451`; PR `#81516` covers only sessions bound before the cron mutation and does not remove cron's process-global override, writer-preferring lock, or sequential workdir pool. PR `#61976` is directionally related but broader. Current upstream history still contains the lock-based implementation as of 2026-08-27.
- **Upstream PR:** Related: #81516 and #61976 (open; checked 2026-08-27).
- **Regression:** `scripts/run_tests.sh tests/gateway/test_gateway_cron_cwd_isolation.py tests/gateway/test_async_delivery_capability.py tests/agent/test_runtime_cwd.py tests/agent/test_prompt_builder.py tests/cron/test_cron_workdir.py tests/cron/test_parallel_pool.py tests/tools/test_file_tools_cwd_resolution.py tests/tools/test_terminal_task_cwd.py tests/tools/test_code_execution_modes.py -q`. The cron regression runs 2 real concurrent job contexts with different `AGENTS.md` and marker files, proves overlap, and checks prompt/context, remote-backend probe caching, terminal, file, code-execution, delegation, and unchanged process `TERMINAL_CWD` values.
- **Rollback:** Revert `fix(cron): isolate workdirs without global state` independently if concurrent cwd isolation fails, restoring the lock, sequential pool, and lock-failure classification while preserving the older gateway isolation patch. Before retirement, prove released upstream behavior under both cron-first gateway ordering and 2 concurrent cron workdirs; verify each workspace's prompt, context files, terminal, file reads, project-mode code execution, and delegation with no cross-session contamination.
- **Additional historical subjects (optional provenance):** `fix(cwd): isolate gateway sessions from cron workdirs`; `fix(reconcile): preserve cwd and timezone contracts`; `fix(reconcile): preserve scoped cwd and canonical title contracts`.

### HERMES-022 — Job-scoped native outbound messages for cron

- **Summary:** Restores `send_message` as a default-off `messaging` toolset and lets a single cron job opt in with `allow_messaging=true`. The opted-in job may send multiple native messages through the configured Hermes adapter identity, but only to the job's bound origin. Each newly acquired fire claim receives a fresh durable run identity; a duplicate delivery remains idempotent while the original claim is live. Pre-send `queued` records are reclaimable; an atomic transport-start fence becomes `ambiguous` before adapter invocation, preventing duplicate retries after an uncertain send. Adapter errors remain ambiguous unless explicit `delivery_stage="pre_send"` evidence proves transport never began. `[SILENT]` suppresses only the scheduler's final automatic delivery, and the tool cannot select another account, profile, or chat ID. Profile-bound live-adapter sends deliver extracted `MEDIA:` attachments through the adapter's native typed senders (images batch, voice, document; `[[as_document]]` forces document routing) and report a partial-delivery ambiguous error — never a verified success — when an attachment fails after the text was delivered (2026-08-27 correction: the live path previously dropped attachments silently).
- **Partial transport evidence:** Validation failure after an earlier attachment send retains every confirmed media ID, including invalid descriptors, missing files and unsupported native methods. Preserve those receipts when reconciling upstream media helpers, and never reinterpret a positive partial count as whole-send success. `tests/tools/test_send_message_tool.py::TestPartialDeliveryReceipts` owns this boundary.
- **Narrowed 2026-08-31 (media implementation retired to upstream):** the fork's private inline media path in `_send_via_adapter._send_live` was replaced by upstream's shared `_send_live_adapter_media`, which owns caption splitting, video/voice routing, per-descriptor validation (including a real `os.path.exists` check) and the inherited-no-op guard. Three behaviours remain fork-owned and are re-expressed on top of that helper rather than beside it: (1) gateway-owned loop dispatch, unchanged, still proven by `TestSendGate`; (2) the ambiguous partial-delivery report, now carrying `media_partial_count` instead of `media_delivered` because on a failure the latter reads as a success claim and upstream asserts its absence; (3) image album batching via `send_multiple_images`, preserved for adapters implementing it natively (Telegram) because upstream's per-descriptor delivery turns a 3-image album into 3 separate messages. Adopting upstream's success shape changed this patch's contract deliberately: `message_id` is now the last media message and `media_delivered` is boolean. Fork tests asserting the retired implementation's routing were deleted rather than ported, per the lifecycle rule on duplicate tests; `TestLiveAdapterMedia` now covers only the three retained behaviours.
- **Surfaces:** `cron/jobs.py`; `cron/scheduler.py`; `cron/outbound.py`; `tools/send_message_tool.py`; `tools/cronjob_tools.py`; `toolsets.py`; `tests/cron/test_cron_outbound_messages.py`; `tests/cron/test_scheduler.py`; `tests/cron/test_jobs.py`; `tests/cron/test_cronjob_schema.py`.
- **Upstream tracking:** Direct PR `#86648` implements this patch contract on current upstream `main` and links issues `#20140` and `#67591`. Its 2026-08-16 automated review raised three items. The edit-preservation concern was valid as a missing regression: the CLI already forwards `None` without coercion and the update boundary already treats it as “unchanged,” now proven by an unrelated timezone edit preserving `allow_messaging=true`. The result-downgrade concern was valid and fixed in the fork by making a durable `verified` record immutable against late callbacks. The toolset-name concern is stale against this fork: `messaging` is an explicit default-off capability, no platform base toolset includes it, and runtime send gates remain a second boundary. Related open PRs `#7388` and `#70304` remain incomplete: `#7388` is a process-wide env var, `#70304` restores a broader platform-level messaging toolset, and neither provides job-scoped origin-only targeting plus idempotent multi-message delivery. Removal context: merged PR `#47856`.
- **Upstream PR:** Direct: #86648 (open, no reviews or review threads, one automated issue comment addressed as classified above, head checks not reported; checked 2026-08-18). Related: #7388 (open, failing head checks, one unresolved review thread) and #70304 (open, failing head checks, no unresolved review threads). Removal context: #47856 (merged).
- **Regression:** `scripts/run_tests.sh tests/cron/test_cron_outbound_messages.py tests/cron/test_scheduler.py tests/cron/test_jobs.py tests/cron/test_cronjob_schema.py -k 'disabled_toolsets or memory_toolset or PerJobToolset or allow_messaging or new_fire_after_stale_claim'`.
- **Rollback:** Remove the `allow_messaging` field, the cron outbound ledger, the cron-only `send_message` registration/check, the origin-only send gate, the `[SILENT]` exception for explicit outbound messages, and the HERMES-022 tests. Restore the previous default cron denylist and delivery hint. Do not restore a process-wide messaging env var.
- **Retirement:** Retire after released upstream provides job-scoped opt-in, origin-only targeting, adapter-owned identity, idempotent multi-message delivery, and `[SILENT]` that does not suppress those explicit messages.
- **Additional historical subjects (optional provenance):** `feat(cron): job-scoped native outbound messages`; `fix(fork): preserve reconciled patch contracts`; `fix(cron): reconcile outbound retry states`; `docs: record upstream PR feedback state`; `fix(cron): keep outbound adapter profile-bound`; `fix(cron): preserve outbound run identity across retries`; `fix(cron): mint fresh run identity per fire claim`; `fix(cron): recover pre-send outbound claims`; `fix(cron): preserve ambiguous post-send errors`; `fix(cron): bind native sends to owning profile`; `fix(review): restore backup and cron ownership fences`; `fix(review): fence cron ownership and profile-bound sends`; `fix(review): narrow origin normalization and fail closed on profile`; `fix(review): commit complete malformed backup bundles`; `fix(review): revoke stale cron outbound authority`; `fix(review): atomically fence cron transport and backup reuse`; `fix(review): fence outbound claims and validate source manifests`; `fix(review): stabilize backup publication and profile fallback`; `fix(review): validate final bundles and route trusted profiles`; `fix(review): recover interrupted manual cron executions`; `test(cron): bind trusted owner in outbound fixtures`; `fix(review): restore upstream cron and title contracts`; `fix(cron): keep verified outbound results immutable`; `fix(cron): deliver media on profile-bound sends`; `fix(review): harden profile-bound media and curation readback`; `fix(cron): fail closed on unresolved job profiles`; `fix(cron): support profile-bound standalone sends`; `fix(cron): preserve profile media-only sends`; `fix(cron): reconcile profile-bound media with upstream helper`.

### HERMES-036 — Allow per-job local memory opt-in for cron

- **Summary:** Lets a cron job explicitly add the local file-backed `memory` toolset while cron continues to run with `skip_memory=True`, so external memory-provider hooks remain disabled.
- **Surfaces:** `cron/scheduler.py`; `tests/cron/test_scheduler.py`; `tests/agent/test_skip_memory_store_65429.py`; cron documentation.
- **Upstream tracking:** No equivalent released per-job local-memory opt-in was identified through upstream `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/cron/test_scheduler.py tests/agent/test_skip_memory_store_65429.py -k 'memory_toolset or local_user_memory'`.
- **Rollback:** Remove only the explicit `memory` allowlist path and its focused tests; preserve cron's default memory-provider suppression and all unrelated per-job toolsets.
- **Retirement:** Retire after released upstream permits the same explicit local-memory toolset without invoking external provider hooks and passes the focused regressions.
- **Additional historical subjects (optional provenance):** `feat(cron): allow local memory opt-in`; `test(cron): verify local USER memory writes`; `fix(reconcile): preserve cron and doctor contracts`.

### HERMES-057 — Retired total cron run budgets

- **September 15 retained-worker ownership repair:** The independent inactivity watchdog now carries the exact worker Future into durable finalization. Completion waits for that worker to exit while fire/run heartbeats retain ownership. Late results do not replace the timeout, and a replacement owner remains fenced from stale completion. Real recurring and one-shot tests verify durable state and competing-process exclusion. This retains a scheduler worker slot while execution lingers and does not restore total run budgets. Roll back the Future handoff and matching lifecycle regressions together.

- **Summary:** Historical optional per-job total wall-clock cap across cron persistence, model-tool and CLI surfaces, pre-agent work, agent execution, and teardown.
- **Upstream tracking:** Historical issue #79244 and PR #79880 did not provide an equivalent per-job contract; upstream commit `803397e` supplies only the separate native agent-level `run_budget_seconds` behavior, which remains intact.
- **Upstream PR:** Historical related PR #79880; no upstream replacement is claimed for this user-directed retirement.
- **Retired:** `refactor(cron): remove total run budgets (#70)` (2026-09-06). User-requested source retirement; legacy `run_budget_seconds` keys remain inert until active jobs are cleared through the native API. The independent inactivity watchdog and durable claim/worker-ownership protections remain active.
- **Rollback:** Git history preserves the retired implementation. Do not restore it without new explicit authorization.
- **Additional historical subjects (optional provenance):** `fix(cron): enforce total run budgets`; `fix(cron): enforce monitor read deadlines`; `fix(cron): retain claims until timed-out workers exit`; `fix(cron): bound teardown while retaining worker tombstones`; `fix(cron): preserve teardown after budget exhaustion`; `merge: land reviewed cron cleanup fix for release`.

## September 15 capture and recovery follow-up

Timezone-only edits recompute and invalidate pending occurrences only for cron wall-clock schedules. Interval and absolute one-shot schedules retain existing due times and recovery slots unless their schedule or lifecycle is explicitly changed. Real persisted-job tests cover timezone changes/clears and combined schedule edits.

Resubmitting an unchanged schedule and normalized timezone, as the desktop editor
does on rename, preserves both unclaimed slots and durable deferred occurrences.
Actual schedule changes, cron timezone changes and explicit lifecycle rewrites
still supersede pending work. Real script/job/ledger tests prove the original
deferred instant remains claimable after the catch-up window.
