# Memory and skill source ownership

This responsibility covers profile-aware memory providers, source supersession, knowledge tools and skill discovery/review. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

## Coupled current adaptations

Named child execution now carries its read-only knowledge mode into the worker
thread, where the existing context and subprocess environment propagation enforce
first-party mutation restrictions. Memory-history rollback checks that authority
before reading or writing history/store state. A real named AIAgent executed via
`_ChildRun.await_child` verifies direct rollback, the persistent `execute_code`
subprocess and the terminal rollback CLI all refuse mutation, while parent and
ordinary unnamed callers retain their existing behavior. Coverage lives in
`tests/tools/test_memory_history_named_child.py`. This closes the first-party API
and execution-context gap, not arbitrary Python or shell filesystem confinement.

An unnamed descendant of a named child already inherits read-only knowledge
context through nested context entry and copied worker context. Toolset selection
also preserves inherited skill-management denial. The contrary review allegation
was disproven with real nested admission, AIAgent construction, daemon execution
and registry writes in `tests/tools/test_delegate_nested_knowledge.py`. Both
named and unrestricted parent controls are covered. This adds regression evidence
without changing production authority or claiming arbitrary-code confinement.

Imported cron-memory tests retain HERMES-036's explicit local-memory opt-in and
external-provider suppression. This test adaptation does not change production memory policy.

## Self-learning review policies — source candidate

- **Stable subject:** `feat(review): add observation policies and recoverable memory writes`.
- **Contract:** `website/docs/developer-guide/self-learning-policies.md` documents direct/observe/off skill review, automatic/approve_changes/observe_only unattended built-in memory, private idempotent evidence consumption, and conflict-safe rollback. Missing memory policy retains approve_changes; /refine remains attended for memory but respects skill observe/off. Preserve upstream #106310 trigger protections for #105921. Upstream #106918/#106919 are proposals, not acceptance.
- **Surfaces and regression:** background review/skill and memory tool boundaries, profile-local observation/history modules, shared `/memory` display; `tests/tools/test_{self_learning_policies,memory_policy,review_policy_boundaries}.py` plus whole memory/skill/approval/review test modules. No new model tool or provider integration.
- **Rollback/retirement:** revert the scoped source commit without deleting retained history, observations or pending writes. Retire only after released upstream passes the complete documented contracts. Source publication/landing only: no runtime promotion, restart, config activation, or live memory edits.

- Source admission releases its exact token after SDK 401/403 and structured pre-route validation 422, only before a submission response. Generic errors, lost responses and missing operation receipts remain unresolved. Server `not_found` does not prove terminal eviction; there is currently no supported settlement CLI. Regression: `tests/plugins/memory/test_source_supersession.py`. See provider README for the deliberate availability limit and recovery constraints.

- Known pre-request memory admission failures release source reservations without corrupting earlier completion evidence. Ambiguous remote outcomes remain reserved. Regression: `tests/plugins/memory/test_source_supersession.py`.

- Source replacement admission uses the existing SQLite journal across providers sharing one home, endpoint and bank. Deferred versions retain their bytes after submission errors. Unreceipted submissions never expire or replay automatically, and only positive terminal operation evidence permits successors. Journal refresh preserves newer in-memory acceptance after disk errors. Separate homes/hosts remain uncoordinated, deferred bytes remain process-local, and unknown submissions require operator reconciliation. Regression: `tests/plugins/memory/test_source_supersession.py`. Rollback must preserve the journal and unresolved reservations rather than treating them as failed requests.

- Fresh same-source memory writes serialize through the existing retention drain. The in-memory queue preserves distinct unsubmitted versions in last-observation order, including A/B/A reversions, and never reconstructs payloads from journal references. Only positively terminal prior operations permit another write. Source-status 404 without previously observed completion remains unresolved even if current content matches. Deferred bytes can be lost on process exit and accumulate while a source stays blocked. Regressions: `tests/plugins/memory/test_source_supersession.py`. Roll back the queue and missing-status boundary together without deleting historical evidence.

- Memory undo history validates record identity, target, content fingerprints and sortable timestamps before recovery. Malformed records do not hide valid undo records, and target-locked revalidation refuses changed records. Regression: `tests/tools/test_memory_policy.py`. Rollback does not delete or rewrite retained history.

- Hindsight profile-env drift stays scoped to managed keys, now gated by upstream's keyless fail-closed check so a scopeless process neither rewrites nor restarts the daemon.

- Restore the existing blank-slate tool overlap filter removed incidentally by upstream's Collective Wisdom revert `0dcadf6f41c`. Disabling another bundle must not strip file, terminal, vision, or skill tools retained by setup. Existing behavioral tests cover this contract.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-006 | Active |
| HERMES-008 | Active |
| HERMES-009 | Active |
| HERMES-010 | Active |
| HERMES-020 | Active |
| HERMES-026 | Active |
| HERMES-038 | Active |
| HERMES-039 | Active |
| HERMES-042 | Active |
| HERMES-056 | Active |
| HERMES-059 | Active |
| HERMES-087 | Active |

## Patch records

### HERMES-006 — Resolve memory notifications per platform

- **Independent wiring hypothesis (2026-09-06):** `TurnRunner._wire_turn_agent_callbacks` bypasses the existing platform resolver, so helper tests pass while a platform `false` override becomes global `on`. Resolve the setting using the live turn's platform, including LOCAL-to-cli mapping, and retain existing boolean/string/default normalization. Replace helper-only tests with the real callback wiring, checking platform precedence and reused-agent refresh. No config migration or memory-retention behavior changes are needed.
- **Summary:** Uses the platform-specific display setting before the global fallback, allowing one platform to disable memory notifications without disabling them everywhere.
- **Surfaces:** `gateway/run_turn_runner.py`; `gateway/display_config.py`; `tests/gateway/test_memory_notifications_per_platform.py`.
- **Upstream tracking:** Narrow backport associated with upstream `#59364`.
- **Upstream PR:** Associated: #59364 (open; rechecked 2026-09-06). It does not release the live callback repair. No supported config or plugin can repair a core callback that bypasses its resolver.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_memory_notifications_per_platform.py`. The real callback tests fail before the repair for platform boolean/string precedence, LOCAL-to-cli routing, and cached-agent refresh, and pass after it.
- **Rollback:** Replace the `resolve_display_setting(...)` call with the released upstream configuration path and remove the private test only after equivalent per-platform precedence is covered upstream. Do not fall back to reading only `display.memory_notifications`.
- **Additional historical subjects (optional provenance):** `chore(local): carry Brian-owned working-tree patches into the fork`; `fix(hermes): restore runtime callsite wiring`.

### HERMES-008 — Preserve Hindsight's explicit shared observation scope

- **Summary:** Preserves an explicit empty inner scope (`[[]]`) so Hindsight performs one shared consolidation pass instead of silently reverting to the combined default.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; `TestObservationScopes` coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** Related upstream issue `#74933`.
- **Upstream PR:** None after checked 2026-08-14; issue #74933 only.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k 'ObservationScopes or shared_scope'`.
- **Rollback:** Remove only the explicit-empty-inner-list preservation branch and its seven patch-owned tests after the released upstream parser proves equivalent handling for native and JSON forms, mixed scopes, whitespace-only entries, provider config, and retain calls.
- **Additional historical subjects (optional provenance):** `chore(local): carry Brian-owned working-tree patches into the fork`.

### HERMES-009 — Fail Hindsight retains on extraction errors

- **Summary:** Sets `HINDSIGHT_API_FAIL_ON_EXTRACTION_ERRORS=true` for embedded profiles so collectors cannot mistake extraction failure for a legitimately empty successful document and advance source cursors.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; embedded-profile environment coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** No equivalent released upstream Hermes behavior was identified when this patch was published; also verify the current Hindsight server contract before retirement.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k embedded_profile_env` plus a failed-extraction operation probe against the supported embedded Hindsight version.
- **Rollback:** Remove the managed environment key only after upstream Hermes/Hindsight guarantees failed extraction yields a failed operation. Update the environment assertion and prove cursor-owning consumers still distinguish failure from an empty document.
- **Additional historical subjects (optional provenance):** `chore(local): carry Brian-owned working-tree patches into the fork`.

### HERMES-010 — Avoid destructive Hindsight daemon restarts and empty-key overwrite

- **Summary:** Compares only Hermes-managed environment keys, ignores daemon-added keys, and preserves a stored API key when live secret resolution is temporarily empty. This avoids restarting the embedded daemon on every session initialization and killing in-flight work.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; embedded-profile drift coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k 'embedded and (env or config or restart)'` plus a daemon restart-count probe across repeated session initialization.
- **Rollback:** Replace the managed-key comparison and preserved-key materialization with the released upstream lifecycle implementation. Remove/adapt only its focused tests after proving daemon-added keys cause no restart and an unavailable secret lookup cannot blank a persisted credential.
- **Additional historical subjects (optional provenance):** `chore(local): carry Brian-owned working-tree patches into the fork`.

### HERMES-020 — Limit background review skill creation

- **Summary:** Adds `skills.background_review_allow_create`, enforced at the `skill_manage` runtime boundary for the `background_review` origin only. `false` blocks `create` while preserving foreground creation and background updates to existing skills.
- **Surfaces:** `tools/skill_manager_tool.py`; `cli-config.yaml.example`; `tests/tools/test_skill_manager_tool.py`.
- **Upstream tracking:** Local fork behavior; no released upstream setting currently provides this selective policy.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/tools/test_skill_manager_tool.py`.
- **Rollback:** Remove the create guard, its focused test, the example setting, and this manifest entry in one follow-up commit. Preserve the existing background ownership and read-before-write guards.
- **Additional historical subjects (optional provenance):** `fix(skills): limit background review creation`; `fix(config): recognize background skill creation policy`; `test(skills): align ledger with background creation policy`; `fix(skills): fail closed on malformed create policy`.

### HERMES-026 — Retain source evidence without implicit raw attachment upload

- **Fresh pending reversion:** Keep the latest freshly observed source in provider memory while earlier same-source writes remain unresolved. Existing retain-drain reconciliation waits for terminal operation evidence before submitting it, then waits for exact readback. Missing status alone cannot authorize that write. A later observation replaces the deferred desire, and terminal bookkeeping is pruned with operation references. Deferred bytes are deliberately not journaled or replayed after process exit, and configurations that disable drain leave them pending until drain runs. Preserve that privacy and lifecycle boundary when adopting upstream reconciliation. Source supersession tests cover write completion order, newer observations, unknown status and restart without payload replay. Revert the queue and terminal-evidence changes together, preserving existing operation journals.

- **Discovery boundary:** Use canonical user-origin projection so synthetic follow-through cannot become a new human turn or source. Deduplicate each source version at its last observation, preserving A/B/A reversion through retention and restart. Quoted JSON and YAML credential assignments remain excluded alongside unquoted assignments. These corrections extend existing discovery ownership, not the provider journal or external-retention permissions. Proof: `tests/plugins/memory/test_source_retention.py` and `test_source_supersession.py`; revert their scoped discovery hunks together if needed, without changing pending-upload recovery.
- **Summary:** Automatically retains substantive source-like pasted text with stable provenance and readback verification. Explicit empty `retain_tags` and `retain_source` values stay empty instead of inheriting environment defaults. Tool-derived webpages, transcripts, file extracts, and written artifacts cross an external-retention boundary and require `retain_tool_sources: true`; retained web provenance never stores URL user information or query values. Generic URLs keep queryless source identity, while an allowlisted public YouTube video key may affect only the opaque source hash so distinct videos do not collide. Raw file attachments additionally require `retain_attachments: true`; generic `read_file` output additionally requires `retain_file_extractions: true`. Discovery accepts attachment bytes only from canonical profile-aware media cache roots; the asynchronous upload boundary reopens with no-follow semantics, revalidates the root and regular-file type, enforces a 25 MiB bound, and verifies the original content hash. All three booleans default to false.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; `plugins/memory/hindsight/source_retention.py`; `tests/plugins/memory/test_source_retention.py`; `tests/plugins/memory/test_source_retention_canary.py`.
- **Upstream tracking:** Local fork behavior; no released upstream equivalent or privacy-gated source-retention contract has been identified.
- **Upstream PR:** None.
- **Regression:** `pytest -q tests/plugins/memory/test_source_retention.py tests/plugins/memory/test_source_retention_canary.py`.
- **Rollback:** Remove automatic source discovery, source-ledger/readback tracking, and HERMES-026 tests together. Preserve ordinary conversation retain, HERMES-008 observation scopes, HERMES-009 extraction-error handling, and HERMES-010 embedded-daemon safety.
- **Retirement:** Retire after released upstream preserves equivalent source provenance and durability while keeping raw attachment reads/uploads default-off behind an explicit trust-boundary opt-in.
- **Additional historical subjects (optional provenance):** `feat(memory): retain source material automatically`; `fix(memory): gate raw attachment retention`; `fix(memory): gate generic file extraction retention`; `fix(memory): constrain retained attachment paths`; `fix(memory): revalidate attachment bytes before upload`; `fix(memory): require consent for tool-derived sources`; `fix(review): close candidate delivery blockers`; `fix(hermes): close verified review gaps (#71)`.

### HERMES-038 — Show the Hindsight thought recall indicator only for retrieved context

- **Summary:** Uses the thought-bubble glyph only when Hindsight actually supplied recalled context, avoiding a misleading provider indicator on turns without retrieval or confusion with Hermes reasoning. The separate background-save indicator remains unchanged. Recall status text uses the provider-neutral `Recalled N memories` wording, preserving each provider glyph, singular/plural counts, and the generic relevant-memory fallback.
- **Surfaces:** `agent/memory_provider.py`; `agent/memory_manager.py`; `plugins/memory/hindsight/__init__.py`; Hindsight README; turn-context, recall-indicator, and Hindsight-provider tests.
- **Upstream tracking:** No equivalent released indicator contract was identified through upstream `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/agent/test_turn_context.py tests/agent/test_memory_recall_indicator.py tests/plugins/memory/test_hindsight_provider.py -k 'indicator or glyph or context'`.
- **Rollback:** Restore upstream's provider indicator behavior and remove only the focused glyph assertions. To revert only the shortened copy, restore the provider-label prefix and lowercase verb in `MemoryManager.describe_recall`, with matching README, setup description, and recall/turn-context test expectations.
- **Retirement:** Retire after released upstream exposes an equivalent truthful retrieved-context indicator.
- **Additional historical subjects (optional provenance):** `feat(hindsight): use brain indicator glyph`; `feat(hindsight): use thought recall indicator`; `fix(memory): shorten recall status label`.

### HERMES-039 — Keep memory retrieval guidance capability-aware with durable replay

- **Summary:** Distinguishes semantic memory from transcript/session search. Explicit Hindsight recall includes provenance by default. Entity context, source chunks, and supporting facts are opt-in. Automatic context uses a persisted `api_content` sidecar for exact historical replay while keeping transcript `content` clean. Observation-only retrieval matches upstream. Mixed `recall_types` remain available without a `prefer_observations` filter, so returned raw evidence is not suppressed merely because an observation summarizes it. The audited curation tools (`hindsight_invalidate`/`hindsight_restore`) await the 0.9.1 client's async `memory.update_memory`/`get_memory` calls and fail closed unless the server readback positively reports the requested state.
- **Surfaces:** `agent/memory_provider.py`; `agent/prompt_builder.py`; `agent/system_prompt.py`; `agent/conversation_loop.py`; `agent/turn_context.py`; `plugins/memory/hindsight/__init__.py`; `tools/session_search_tool.py`; focused prompt, sidecar, and Hindsight tests.
- **Upstream tracking:** Upstream observation-only defaults and `api_content` replay are the baseline. Remaining fork-only behavior is provenance-on/entity-off explicit recall plus opt-in verified invalidate/restore.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/agent/test_prompt_builder.py tests/agent/test_api_content_sidecar.py tests/agent/test_gateway_turn_sidecar.py tests/agent/test_turn_context.py tests/plugins/memory/test_hindsight_provider.py`. The curation correction is covered by `tests/plugins/memory/test_hindsight_provider.py -k TestMemoryCuration`.
- **Rollback:** Restore request-scoped injection, entity-on explicit recall, and `prefer_observations` together; preserve upstream message alternation and ordinary memory-provider lifecycle.
- **Retirement:** Retire remaining fork-only recall expansion and curation after released upstream provides equivalent provenance-rich explicit recall and verified invalidate/restore.
- **Additional historical subjects (optional provenance):** `feat(memory): clarify semantic and transcript recall guidance`; `feat(memory): prefer observations and curate hindsight memories`; `feat(memory): expand explicit recall context by default`; `fix(context): keep retrieved provider context request-scoped`; `refactor(prompt): scope retrieval guidance by capability`; `test(memory): align request-scoped gateway turn context`; `fix(memory): await hindsight curation calls`; `fix(review): harden profile-bound media and curation readback`; `refactor(memory): adopt upstream replay and lean recall defaults (#69)`.

### HERMES-042 — Pin the supported Hindsight 0.9.1 client contract

- **Summary:** Pins `hindsight-client==0.9.1`, aligns packaging with that API, and keeps Hermes-only provenance formatting out of Hindsight `arecall` kwargs so explicit recalls remain expanded without emitting one unsupported-field warning per call.
- **Surfaces:** `pyproject.toml`; `uv.lock`; `tools/lazy_deps.py`; `plugins/memory/hindsight/__init__.py`; Hindsight metadata, docs, and tests.
- **Upstream tracking:** Upstream `5dd15872a6` still pins Hindsight client 0.6.1; no released equivalent 0.9.1 integration was identified on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `uv lock --check`; `scripts/run_tests.sh tests/plugins/memory/test_hindsight_provider.py tests/test_packaging_metadata.py`; explicit recall regression proving `include_provenance` controls Hermes formatting but is absent from `arecall` kwargs.
- **Rollback:** Revert the client pin and matching API adaptations as one unit; regenerate `uv.lock` with the repository-supported uv and preserve unrelated dependency updates.
- **Retirement:** Retire after released upstream supports the same or newer compatible Hindsight API and passes packaging plus provider regressions.
- **Additional historical subjects (optional provenance):** `fix(memory): pin hindsight client to 0.9.1`; `fix(memory): keep provenance out of Hindsight API kwargs`.

### HERMES-056 — Trust configured external skill symlink farms

- **Independent hypothesis (2026-08-27):** `skill_view` resolves both the selected `SKILL.md` and every trusted root before containment checks. A managed external root whose direct skill entries are symlinks into a generated build cache therefore emits `outside the trusted skills directory` on every valid load even though the unresolved entry is lexically inside an explicit `skills.external_dirs` root. The correction belongs only at that explicit external-root boundary: accept lexical containment there while retaining resolved containment for profile and project roots, so an unconfigured local symlink escape still warns.
- **Surfaces:** `tools/skills_tool.py`; `tests/tools/test_skills_tool.py`; this record.
- **Upstream tracking:** No equivalent released behavior or matching local upstream-history change found as of 2026-08-27.
- **Upstream PR:** None checked 2026-08-27.
- **Regression:** `pytest -q tests/tools/test_skills_tool.py`; the managed external symlink-farm case loads without a warning, while the local/profile symlink escape case still emits the warning.
- **Rollback:** Revert only `fix(skills): trust configured symlink farms`, restoring resolved-only trust classification and its previous false positive. Preserve skill traversal, collision, mutation, and quarantine guards.
- **Retirement:** Retire after a released upstream version distinguishes explicitly configured external symlink farms from unconfigured local symlink escapes with equivalent positive and negative regressions.

### HERMES-059 — Keep initialized memory-provider tools routable

- **Independent hypothesis (2026-08-28):** `MemoryManager.add_provider()` indexes tool routes before `provider.initialize()` loads provider configuration, while `inject_memory_provider_tools()` asks for schemas after initialization. A provider whose schemas expand or change during initialization can therefore advertise tools that are absent from `_tool_to_provider`; `tool_executor` then skips the memory-provider branch because `has_tool()` is false and returns `Unknown tool`. The correction belongs in `MemoryManager`: rebuild the complete routing table from initialized schemas, preserve normalization, reserved-core-name rejection, first-provider conflict precedence, and stale-route removal, then expose only schemas whose route maps back to the same provider.
- **Summary:** Rebuild memory-provider routing after initialization so configuration-gated Hindsight curation tools are callable, remove routes for schemas withdrawn during initialization, and preserve the invariant that every advertised provider tool is routable to its owning provider. The follow-up hardening excludes providers whose initialization failed, makes conflict checks independent of provider truthiness, proves per-provider generator rollback and the complete `AIAgent` injection/dispatch path, and adds bounded route-delta observability.
- **Surfaces:** `agent/memory_manager.py`; `tests/agent/test_memory_provider.py`; `tests/agent/test_memory_provider_init.py`; this record.
- **Upstream tracking:** Open issue #58360 reports the same advertised-but-unroutable lifecycle class. Open PRs #9223 and #16089 propose post-initialize refreshes; closed duplicate PR #94588 supplies the stronger full-rebuild shape. PR #16089's additive loop was reviewed as incomplete because it bypasses normalization and reserved-name protection and leaves stale routes. No released upstream implementation satisfies the complete contract as of 2026-08-28.
- **Upstream PR:** Direct: #9223 and #16089 (open); closed duplicate/reference implementation: #94588.
- **Regression:** Initial patch: RED lifecycle tests proved the advertised/unroutable mismatch, stale routes, and missing late tools; GREEN focused memory tests, lint, manifest validation, live Hindsight canary, and independent review. Follow-up hardening: five focused tests produced three expected RED failures for failed-initialization routing, falsy-provider conflict precedence, and missing delta logs while generator rollback and the existing agent path passed; after implementation all five passed. `scripts/run_tests.sh tests/agent/test_memory_provider.py tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_provider_init.py -q` passed `189` tests; `uv run ruff check agent/memory_manager.py tests/agent/test_memory_provider.py tests/agent/test_memory_provider_init.py`, `python scripts/validate_maintenance_manifest.py MAINTENANCE.md`, and `git diff --check` passed; an initialized live-config Hindsight canary reported `hindsight_invalidate` and `hindsight_restore` both advertised and routable. Independent Claude autoreview exited `0` with no accepted or actionable P0/P1 findings; its speculative post-initialization `add_provider()` edge is outside Hermes's intentionally frozen session tool surface.
- **Rollback:** Revert `fix(memory): harden initialized provider routing` first, then revert `fix(memory): keep initialized provider tools routable`, restoring registration-time-only routing and removing the focused regressions plus this record. Preserve Hindsight mutation schemas, provider configuration, and all unrelated memory-provider toolset gates.
- **Retirement:** Retire after released upstream rebuilds provider routing after initialization, removes stale routes, preserves normalization/core-name/conflict behavior, advertises only dispatchable schemas, and passes equivalent lifecycle regressions.

### HERMES-087 — Stop duplicating root skill names

- **Summary:** Classify only genuinely nested skill paths by their parent hierarchy, place root-level skills under `general`, and advance the skill-prompt snapshot version so cached self-qualified entries are rebuilt. Categorized and plugin-qualified skills retain their existing behavior.
- **Surfaces:** `agent/prompt_builder.py`; `tests/agent/test_prompt_builder.py`; this record.
- **Upstream tracking:** No dedicated issue found as of 2026-08-31. Open issue #74929 concerns broader skill-invocation enforcement but does not identify this path-classification defect.
- **Upstream PR:** Open draft PR #81338 is the direct source of this implementation and snapshot migration; open PR #26467 implements the same category correction but does not invalidate version-2 snapshots. Preserve contributor authorship from #81338.
- **Regression:** `scripts/run_tests.sh tests/agent/test_prompt_builder.py tests/tools/test_skills_tool.py tests/test_plugin_skills.py -q`; coverage must prove a version-2 root-skill snapshot rebuilds under `general`, the skill name is not repeated as a category, ordinary top-level and categorized lookup contracts remain aligned, and plugin-qualified skills still resolve.
- **Expected published commit identity:** Stable subject `fix(skills): stop duplicating root skill names`; source, regressions, snapshot migration, and this record ship together.
- **Rollback:** Revert only `fix(skills): stop duplicating root skill names`, restore snapshot version 2 and the prior root-path category expression, remove the focused regression, index row, and this record. No schema, configuration, or persistent-data rollback is required; a later prompt rebuild recreates the prior index.
- **Retirement:** Retire after a released upstream version classifies root-level skills under `general`, invalidates stale self-qualified snapshots, and passes equivalent root, nested, and plugin-qualified regressions. Remove the fork implementation and duplicate test rather than retaining parallel behavior.
- **Source references from initial investigation:** `research/DESCRIPTION.md`; `research/SKILL.md`.

## September 15 boundary correction

The static knowledge path resolver combines literal multi-component pathlib constructors using native absolute-component reset semantics. Destructive ancestor checks therefore see the actual literal target. Dynamic arguments remain unresolved under the existing cooperative first-party guard contract. Tests cover terminal refusal, execute-code dispatch and its cwd-aware execution boundary with disposable sentinels; no arbitrary-Python confinement is claimed.
