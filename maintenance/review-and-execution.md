# Review and local execution boundaries

This responsibility covers native review capture/fallback, file/terminal/Python execution and local knowledge boundaries. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

## Coupled current adaptations

The corresponding metadata fixture now uses the real request builder for all
frozen channels, and the profile-routing mock binds the real replay ingress
guard. The hygiene cancellation regression gates its worker until the host
returns, then verifies deferred cleanup. This replaces a whole-handler latency
assertion that raced the worker's own timeout. These fixture corrections do not
change production behavior or relax the cancellation, routing or credential
contracts.

- Reused local Python kernels reject protected actual cwd before dispatch, including source with no literal filename. Sandbox deadline completion requires positive process-group exit evidence after wrapper loss, with unknown probes retaining existing retries. Regressions: `tests/tools/test_code_kernel.py`, `test_background_deadlines.py`. Rollback preserves checkpoints and reopens these execution boundaries.

- Literal destructive globs protect both ancestors and descendants of knowledge roots. The existing wrapper parser tracks literal `env -C`/`--chdir` forms before analyzing the nested executable. Computed-command exclusions remain unchanged. Regression: `tests/tools/test_knowledge_env_and_glob_descendants.py`. Revert the two parser/containment hunks together to roll back this correction.

- Literal knowledge-command analysis preserves quoted/escaped control-token provenance and interprets POSIX `subprocess` sequences with `shell=True` as shell source in their first element. Direct argv semantics, bounded parsing and the documented computed-command exclusions remain. Regression: `tests/tools/test_knowledge_shell_operand_provenance.py`. Rollback reopens these literal destructive-ancestor gaps.

- Empty resolved session cwd is authoritative for relative file tools; process-wide `TERMINAL_CWD` is consulted only when resolution itself fails. Native Anthropic effort pins validate `output_config.effort`, including effective `extra_body`, using the existing adapter normalization for the selected model. Budget-only reasoning remains exempt. Proof: `tests/tools/test_file_tools_cwd_resolution.py` and `test_subagent_fallback_client_route.py`. Roll back these consumer-boundary changes independently of scope storage and named runtime configuration.

- `resolve_tool_cwd()` remains the delegation cwd authority; it falls through to upstream's `scope_terminal_cwd()`, so multiplexed per-turn terminal scoping is preserved.

- review-candidate capture rejects dirty checked-out submodules in scope at every depth and binds gitlink changes independently of ignore settings; descriptor-dependent capture tests skip where the platform cannot support them.

- Local-check POSIX assertions pin the platform.

- The knowledge boundary tracks literal Python `os.chdir` transitions like shell `cd`.

- review-candidate capture enforces a 2 MiB per-file and 8 MiB aggregate evidence bound with a growth-race guard.

- Local-check resolves `npm.cmd` on Windows.

- Literal cwd transitions inside nested `sh -c`, `python -c`, `os.system` and `subprocess` bodies widen the knowledge-boundary bases.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-028 | Retired |
| HERMES-044 | Active |
| HERMES-046 | Active |
| HERMES-047 | Active |
| HERMES-053 | Retired |
| HERMES-103 | Retired |
| HERMES-110 | Retired |
| HERMES-126 | Active |

## Patch records

### HERMES-028 — Retired referenced-script inspection hardening

- **Retired (2026-08-27):** Brian directed full retirement of the fork's terminal/lifecycle safety hardening in favor of exact upstream behavior. Removed: the POSIX `sh`/`dd` remote script reader, the `execution_only`/`fail_on_unresolved` strict guard modes, the quote-aware `_ShellToken`/`_lex_shell_line` grammar, `partition_heredoc_bodies` with its recursive lifecycle heredoc scanning, and the fork-only regressions in `tests/hermes_cli/test_gateway_restart_loop.py` and `tests/tools/test_terminal_heredoc_background_guard.py`. `cron/lifecycle_guard.py`, `tools/shell_heredoc.py`, and the HERMES-028 hunks of `tools/terminal_tool.py` were restored to the fork's upstream base (`7a1aafb4e1`); upstream's own `contains_gateway_lifecycle_command_or_referenced_script` guard, script reader, and inert-heredoc handling remain the active contract. Removal commit: `refactor(safety): retire fork lifecycle guard patches`.
- **Surfaces:** Historical: `cron/lifecycle_guard.py`; `tools/shell_heredoc.py`; `tools/terminal_tool.py`; `tests/hermes_cli/test_gateway_restart_loop.py`; `tests/tools/test_terminal_heredoc_background_guard.py`.
- **Upstream tracking:** Retirement was a deliberate policy decision, not an equivalence claim: upstream's guard allows patterns the fork failed closed on (e.g. `$RUNNER`, `bash $SCRIPT`). Related upstream work: #85804, #88336, #91604.
- **Upstream PR:** None; the private contract was withdrawn rather than contributed (checked 2026-08-27).
- **Regression:** Upstream-owned `scripts/run_tests.sh tests/hermes_cli/test_gateway_restart_loop.py tests/tools/test_terminal_heredoc_background_guard.py tests/tools/test_terminal_tool.py -q` against the restored files.
- **Rollback:** Restore the historical stable subjects' file states from fork history in one follow-up commit; do not partially restore individual helpers.
- **Additional historical subjects (optional provenance):** `fix: harden gateway runtime boundaries`; `fix(terminal): avoid remote Python lifecycle dependency`; `fix(terminal): skip known remote shell binaries`; `fix(terminal): trust only system shell paths`; `fix(terminal): parse guarded shell grammar safely`; `fix(terminal): preserve shell dialect boundaries`.

### HERMES-044 — Compose and protect final response transforms

- **Compatibility boundary:** The maintained core always supplies `hermes_cli.lifecycle.transform_llm_output`. Its compatibility fallback supports a plugin backend with only the generic dispatcher, whose missing dedicated method raises `AttributeError`. Mixing a current finalizer with an older or replaced core lifecycle module is not a supported package configuration. A real generic-backend reproduction confirms one transform invocation through the existing fallback.

- **Summary:** Composes output-transform hooks at the finalization boundary while preserving a substantive answer when a transform returns empty or non-substantive output.
- **Surfaces:** `agent/turn_finalizer.py`; `gateway/run.py`; `hermes_cli/lifecycle.py`; `hermes_cli/plugins.py`; Telegram adapter; transform-hook and Rich Message tests.
- **Upstream tracking:** No released upstream implementation satisfies the complete transform-composition and answer-preservation contract through `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/test_transform_llm_output_hook.py tests/gateway/test_telegram_rich_messages.py`.
- **Rollback:** Remove the fork transform-composition path and its tests while preserving upstream finalization, plugin lifecycle, and Telegram rendering behavior.
- **Retirement:** Retire after released upstream composes equivalent transforms without permitting empty transform output to erase a substantive answer.
- **Additional historical subjects (optional provenance):** `fix(output): compose and protect final responses`.

### HERMES-046 — Honor the live element-token action schema

- **Summary:** Attaches the captured per-snapshot `element_token` whenever the live cua-driver action schema accepts that field or the action advertises the legacy `accessibility.element_tokens` capability. This preserves stale-element protection and restores element-index actions on cua-driver 0.21.x, which may expose and require `element_token` while omitting the capability label. Drivers whose strict schemas expose neither signal receive no extra field.
- **Surfaces:** `tools/computer_use/cua_backend.py`; `tests/tools/test_computer_use.py`.
- **Upstream tracking:** Open issue #89527 reports the same bare-`element_index` rejection after token support is lost at the Hermes adapter boundary. Open PR #89531 is complementary: it restores vendor capability discovery through strict MCP 2.x models but does not replace the live-schema fallback when cua-driver genuinely omits the legacy capability label. Closed-unmerged PR #91116 and duplicate #91299 implement the same schema-or-capability gate. Open PR #92694 carries an equivalent element-token commit plus an unrelated Linux/X11 foreground-delivery change. Official `main` at `6994851694a98c9078bd90f7bc562f7b33f9bb51` does not contain the schema fallback after checked 2026-08-23.
- **Upstream PR:** Source-equivalent: #91116 (closed unmerged), #91299 (closed as a duplicate), and #92694 (open, mergeable, blocked, no reported checks; checked 2026-08-23). Complementary: #89531 (open, mergeable, clean, green CI; checked 2026-08-23).
- **Regression:** `venv/bin/python -m pytest -q tests/tools/test_computer_use.py -k 'ElementTokenAttachment or CapabilityDiscovery'`; `venv/bin/python -m pytest -q tests/tools/test_computer_use.py tests/tools/test_computer_use_delivery_ladder.py tests/tools/test_computer_use_cua_0_9.py`; `git diff --check`; and a live macOS cua-driver 0.21.0 probe proving a captured token is attached when `supports_input_property("click", "element_token")` is true, the legacy capability is false, and the background AX action is accepted without coordinate fallback.
- **Activation:** Source is published in the maintained fork. A gateway process started before this patch remains on the old imported adapter until a separately authorized restart; source publication is not runtime activation.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later unrelated computer-use changes. Remove the live-schema branch and its schema-only/no-signal regressions, restoring the legacy capability-only gate. Expect cua-driver 0.21.x element-index actions to fail with `snapshot_id_required`; do not substitute coordinate clicks as the maintained safety contract.
- **Retirement:** Retire after a released upstream version attaches the matching captured token whenever either the live action schema or legacy capability establishes support, sends no unknown field when neither does, preserves stale-snapshot rejection, and passes the focused unit regressions plus a real background AX canary on the supported cua-driver version. Remove the private implementation and duplicate tests rather than retaining both paths.
- **Additional historical subjects (optional provenance):** `fix(computer-use): honor live element-token schema`; `docs(maintenance): register element-token schema patch`.

### HERMES-047 — Preserve private computer-use runtime readiness

- **Summary:** Narrowed 2026-08-26 during the upstream reconciliation. Released upstream now owns the macOS signed CuaDriver.app launch path (`_resolve_cua_driver_app_path`, `_validate_cua_driver_app_signature` with identifier `com.trycua.driver` / team `4YEC26S9KF` and the `computer_use.allow_unsigned_driver` escape hatch, `_embedded_daemon_spawn_command`), so the private launch/identity/cleanup implementation and its fork tests were removed in favor of upstream during the rebase. The remaining fork behavior is the readiness-probe contract: each private-daemon status probe may take up to `_STATUS_PROBE_TIMEOUT_SECONDS` (5.0s), bounded by the remaining overall startup budget, instead of upstream's fixed 2.0s per-probe bound that permanently fails a healthy-but-slow status client.
- **Surfaces:** `tools/computer_use/cua_backend.py`; `tests/tools/test_computer_use_cua_0_10_permissions.py`; this record.
- **Upstream tracking:** Direct Hermes issue #93312 covers the readiness timeout. Direct Cua issue trycua/cua#3347 covers cua-driver 0.21's eager Computer History attestation latency. Upstream absorbed the macOS TCC launch-identity contract (previously tracked via #84033/#76433); its current `_EmbeddedCuaDaemon.start` still probes with a fixed 2.0s subprocess timeout as of upstream `7a1aafb4e1da` on 2026-08-26. Related open PR #76686 bounds startup but leaves the inner two-second probe unchanged.
- **Upstream PR:** None for the probe-timeout correction after checked 2026-08-23. Related only: #76686.
- **Regression:** `.venv/bin/python -m pytest tests/tools/test_computer_use_cua_0_10_permissions.py -q` including `test_private_daemon_allows_slow_healthy_status_probe`, which fails on the fixed 2.0s bound and passes with the bounded 5.0s probe.
- **Published commit identity:** Stable subject `fix(computer-use): allow slow healthy readiness probes`; the historical subjects `fix(computer-use): preserve private macOS runtime readiness` and `fix(computer-use): keep forced stop portable` remain indexed for fork-history attribution after their launch-path implementation was retired in favor of upstream.
- **Activation:** Source publication is separate from runtime promotion. The active gateway keeps its imported pre-patch adapter until a separately authorized restart; the upstream-owned macOS launch path also needs a live macOS canary during that promotion.
- **Rollback:** Revert `fix(computer-use): allow slow healthy readiness probes` to restore the fixed 2.0s probe. Do not restore the retired private launch-path implementation; if upstream's signed-app launch regresses, repair or backport upstream's implementation as one coherent contract.
- **Retirement:** Retire after released upstream tolerates healthy private readiness clients beyond two seconds within the bounded startup budget and passes the focused slow-probe regression.

### HERMES-053 — Resolve CuaDriver.app through driver symlinks (Retired)

- **Disposition 2026-09-05:** No private behavioral gap remains. Upstream implementation `6662b3618d22f03102cecf203772e1238465306d` and security tests `164db901e8704f7370261c67981c88b968781e86` are included in released tag `v2026.8.31` (`29112bef099274229cadff79cdff7bf7b99c4b77`). Both were already ancestors of the private patch's parent. The old unresolved-path/single-team hypothesis does not describe the reconciled source.
- **Upstream tracking:** Implementation and equivalent security regressions are present in released `v2026.8.31` and current upstream `b51c055a12220f8c7c18660e8599365012e19532`.
- **Upstream PR:** Retirement is grounded in the verified implementation and test commits above, not an open proposal. No additional PR is required for this private bookkeeping cleanup.
- **Preserved contract:** Resolve the selected driver through `os.path.realpath`, derive only its carrying app bundle, require exact bundle ID `com.trycua.driver`, and accept official teams `4YEC26S9KF` and `YCK386LBJ7` without weakening signature verification or mismatched-team rejection.
- **Source retirement:** Restores only the comments, local variable spelling, tuple order and error-string wrapping from private commit `89d1d7ccb7fdf84fe1e4dda7f88c597159f17463`. No resolver, launch, signature validation or upstream-owned regression test is deleted. Current upstream has moved the implementation to `tools/computer_use/cua_backend_daemon.py`.
- **Regression:** `python -m pytest -q tests/tools/test_computer_use_cua_macos_identity.py`. Independent baseline and current-upstream runs each passed 13 tests. Keep this upstream-owned suite, including symlink resolution, both official teams, impostor bundle/team rejection and signature failure handling.
- **Rollback:** Revert only the cosmetic retirement commit if necessary. Never restore the obsolete description's unresolved-path/single-team behavior, which was not present in the private commit's parent.
- **Additional historical subjects (optional provenance):** `fix(computer-use): resolve app bundle through driver symlinks`; `refactor(computer-use): retire redundant driver identity patch`.

### HERMES-103 — Candidate safety umbrella (Retired)

- **Historical scope:** This review-driven umbrella hardened cron ownership and verifier entry, restart recovery, delivery accounting, goal authorization, Telegram/Slack egress, title-call attachment privacy, retained-source redaction, browser snapshot cleanup, run-budget teardown, and trusted fork-history policy.
- **Retirement boundary:** The uniquely owned residual implementation was removed at source level. Shared behavior and infrastructure that is independently required by active patches remains owned by HERMES-004, HERMES-017/HERMES-018, HERMES-022, HERMES-026, HERMES-071, HERMES-091, HERMES-097, and HERMES-102; workflow gates and exact trusted-history validation remain intact and were not weakened to land this retirement.
- **Removed surfaces:** HERMES-103-only restart-inbox exact-turn-token binding beyond HERMES-097, profile-send partial-delivery evidence owned only by this umbrella, and their duplicate focused regressions.
- **Preserved surfaces:** Per-session headed-mode selection and restart recovery are consolidated under HERMES-091; auxiliary title-call attachment privacy remains under HERMES-017/HERMES-018; retained-source URL credential stripping and consent boundaries remain under HERMES-026; HERMES-057 remains retired with no active total-run-budget behavior; registered cron, goal, delivery, browser identity, and CI policy contracts remain unchanged where another patch owns them.
- **Upstream tracking:** None. This was fork-only review hardening and was retired by explicit owner direction rather than replaced by an upstream release.
- **Upstream PR:** None.
- **Published commit identities:** Historical stable subjects `fix(maintenance): close candidate safety gates`; `fix(review): close candidate delivery blockers`; `fix(review): harden reconciled candidate boundaries`; `fix(review): close remaining candidate boundaries`; `fix(review): enforce final candidate invariants`; `fix: close maintenance review findings`; `fix(review): close exact-candidate full-suite failures`; `fix(review): close independent candidate findings`; `fix(review): resolve exact candidate P1 findings`; `fix(gateway): close exact-candidate review findings`; `fix(review): harden profile and queue isolation`; `fix(review): close CI and Telegram flood races`; `fix(review): preserve bounded resumable delivery`; `fix(test): tolerate minimal send results`; `fix(review): close platform goal and daemon gaps`; `fix(review): harden checks rollback and interruption`; `fix(test): preserve terminal exit and route metadata`; `fix(security): authenticate internal wake turns`; `fix(review): fence workers and recovery claims`; `fix(review): bound cleanup and stage backups`; `fix(review): close exact-candidate blockers`; `fix(review): preserve resumable delivery and goal authority`; `fix(review): harden final privacy and authority gates`; `fix(review): enforce final negation and cron invariants`; removal subject `refactor(hermes): retire HERMES-103 patch family (#87)`.
- **Regression:** `scripts/run_tests.sh tests/agent/test_title_generator.py tests/gateway/test_restart_inbox.py tests/plugins/memory/test_source_retention.py tests/tools/test_slack_send_message_media.py -q`; the repository workflow-policy, maintenance-manifest, Windows, JavaScript, and full Python gates remain mandatory.
- **Retired:** `refactor(hermes): retire HERMES-103 patch family (#87)` (2026-09-07). User-authorized source retirement after hunk-level overlap review; no runtime was deployed or restarted.
- **Rollback:** Revert only the removal commit to restore the historical residual implementation. Do not revert shared commits or weaken independently owned gates.
- **Additional historical subjects (optional provenance):** `fix(maintenance): resolve complete-review lifecycle findings`; `merge: reconcile named worker rotation and trusted baseline`; `fix(maintenance): stabilize cancellation and full-suite QA`; `fix(review): stabilize exact candidate contracts`; `merge: reconcile official upstream through 5bd439d3ed4`; `merge: preserve registered gateway restart integration`; `merge: reconcile owned source through 43f35be039b`; `merge: reconcile owned source through 408ac8857d9`; `merge: reconcile managed browser handoff`; `fix(ci): preserve trusted lint workflow during sync`; `merge: reconcile autonomous goal lifecycle`.

### HERMES-110 — Retired Astra instruction support

- **Summary:** Historical Astra-specific prompt, memory-context, compaction-prefix, and bundled cron-blueprint hardening from PR #54. The implementation and duplicate regressions were completely removed at the user's request before any runtime promotion. No compatibility shim or retired production logic remains.
- **Surfaces:** Historical only: `agent/prompt_builder.py`; `agent/memory_manager.py`; `agent/context_compressor.py`; `cron/blueprint_catalog.py`; their focused tests; this record.
- **Upstream tracking:** None. This was fork-only work and is intentionally retired.
- **Upstream PR:** None.
- **Regression:** `scripts/run_tests.sh tests/agent/test_prompt_builder.py tests/agent/test_streaming_context_scrubber.py tests/agent/test_summary_prefix_semantics.py tests/cron/test_blueprint_catalog.py tests/test_maintenance_manifest_validator.py -q`; byte-identity checks compare the retired production surfaces with the pre-Astra first-parent baseline while preserving later independent memory-label work.
- **Published commit identity:** Historical stable subjects `fix(prompt): keep runtime guidance scope-bound` and `fix(prompt): preserve persistent memory after compaction`; removal subject `revert(astra): retire instruction support patches`.
- **Rollback:** The removal commit restores the pre-Astra source behavior and removes the PR #58 suffix-normalization validator logic. Do not restore the retired patch without new explicit authorization. The exact PR #58 subject remains registered as an administrative history exemption because Git history is immutable. No schema, configuration, profile, workflow, or runtime change is required.
- **Retired:** `revert(astra): retire instruction support patches` (2026-09-05). Source-only retirement requested by the user; the active runtime was not promoted from PR #54, so no emitted compaction prefix or deployed compatibility state required preservation.

### HERMES-126 — Ordered Native Review Fallbacks

- **Upstream tracking:** https://github.com/NousResearch/hermes-agent/issues/105388
- **Upstream PR:** https://github.com/NousResearch/hermes-agent/pull/105416
- **Contract:** Translate configured review fallback routes into the native delegated recovery chain. Keep ordinary unpinned parent fallback inheritance intact. No global configuration or runtime activation is part of this patch.
- **Source:** `agent/review_engine.py`, `tools/delegate_tool.py`, `tools/delegate_tool_config.py` and focused regression tests.
- **Verification:** `scripts/run_tests.sh -j 4 tests/tools/test_delegate_runtime_fallback_inheritance.py tests/agent/test_review_engine.py tests/tools/test_delegate.py` passed 113 tests. Independent review's inheritance finding was reproduced and fixed, then same-lineage confirmation passed.
- **2026-09-09 sync — model arm reconciled:** Upstream landed the pin as `override_provider or override_base_url or model`, plus a new decision table `tests/tools/test_delegate_fallback_matrix.py` (#80450, #65038) whose model-only cell expects a pinned child NOT to inherit. Because every delegated child is constructed with a concrete resolved `model`, adopting that expression verbatim erases parent fallback inheritance for ordinary children — the exact regression this record was opened to fix. The two tests are not actually in conflict: upstream's cell points the child at a DIFFERENT model (`deepseek-chat` against a `anthropic/claude-sonnet-4` parent), while `test_delegate_runtime_fallback_inheritance.py` passes the parent's OWN model. Pin is now `override_provider or override_base_url or (model and model != parent_agent.model)`: the parent's chain is recovery policy for the parent's model, so it stops applying once the child moves off it, and a model merely resolved to the parent's own is not a pin. Both suites pass together (21 tests).
- **Retirement:** Remove this adaptation when the upstream review translation and delegated routing behavior satisfy the same ordered-chain and inheritance regressions on the maintained fork.
- **Rollback:** Revert the single commit titled `fix(review): honor configured fallback routes`, including its focused tests and this record. If later delegation changes share these files, remove only the review-owned `routing_cfg` plumbing and translation and rerun the listed tests before publication. Preserve unrelated role and native review policy changes.

## Isolated CI execution boundary

The manual hybrid pilot intentionally permits candidate shell inside an isolated guest. The trusted external supervisor at commit `0668172a8838dc9e1078713929de92cab76dcc44` starts disposable QEMU, then executes GitHub `run.sh --jitconfig` inside that guest over SSH. Workflow steps do not launch the isolation boundary. Recorded guest-root probes and GitHub runner identities corroborate this architecture. Regular Python and JS jobs remain GitHub-hosted. This evidence does not certify production heavy routing or an active runner service.

## September 15 capture and recovery follow-up

Read-only review capture disables Git lazy fetching and every transport for each capture subprocess. Missing promisor objects produce unavailable evidence instead of fetching or executing remote helpers. Real local partial-clone tests verify no transport, object, or index writes, including the protocol fallback for Git versions without the newer lazy-fetch switch.
