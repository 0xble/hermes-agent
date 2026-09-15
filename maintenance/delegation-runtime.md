# Delegation admission, frozen routes and recovery

This responsibility covers named/ordinary child admission, frozen launch metadata, shared routes and durable resume/checkpoint ownership. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

## Coupled current adaptations

The evaluator's named-role scoring deliberately does not translate legacy `role`
into `subagent_type`. Legacy leaf/orchestrator depth semantics do not select a
configured explorer/worker definition. Named selection uses `tasks[].subagent_type`.
`tests/scripts/test_eval_delegation_selection.py` verifies actual normalization and
runtime preflight agree with scoring for both call shapes. No production scoring
change was needed for the contrary review allegation.

Named delegation pins also preserve configured header, query and body override
values at the final SDK request boundary. Checking the child's frozen attribute
alone did not detect middleware changes to the physical request. The check allows
additional builder/SDK defaults, compares header names case-insensitively, and
rejects conflicting spellings of configured headers. It reuses the active frozen
primary/fallback overrides and existing provider-authentication checks. This
preserves configured values, not a blanket ban on arbitrary new request fields.
`tests/agent/test_named_frozen_request_overrides.py` exercises actual configured
preflight, child construction and SDK dispatch with synthetic credentials and
mock transport, proving changed or removed header/query/nested-body values cannot
reach transport while unchanged values and additional defaults can.

The recovery-history review allegation assumed `SessionDB.get_messages()` returns
serialized tool calls. Its existing `_row_to_message_dict` already decodes them.
No production normalization was added. A real persisted-tool regression in
`tests/tools/test_delegation_resume_authorization.py` verifies successful receipts
pass resume preflight and obtain exactly one atomic recovery claim. Missing,
cancelled and malformed histories remain denied at the final claim boundary,
and a newer durable row invalidates the earlier fingerprint. The raw SQLite
representation and decoded public read are both checked without fabricating
receipts or clearing stop authority outside the existing claim transaction.

## Delegated interruption and renewed authorization

Internal cancellation, timeout, stall and error paths carry non-user-stop provenance.
`resume_authorization` is an owning-parent, one-attempt authorization/reconciliation
receipt, guarded by exact durable state, tool history, frozen routing and turn leases.
Never clear stop flags out of band. Regression: `tests/tools/test_delegation_resume_authorization.py`
and the stopped variants of `test_delegation_same_row_resume.py`.
Retire when upstream provides these complete semantics. Reverting source retains
authorization audit; legacy runtimes will again reject stopped children. Activation
requires a separately authorized runtime promotion.

This repository tracks `NousResearch/hermes-agent` while carrying a small set of Brian-owned patches. Official upstream remains authoritative for all unmodified Hermes code. Fork `main` is the last candidate that passed fork verification; runtime promotion is a separate operation.

## Delegation context modes — source candidate

- **Contract:** [conversation context](../website/docs/user-guide/features/delegation.md#conversation-context-context_mode): overridable fresh/fork independent of capability routing; named owner defaults fork, other roles fresh; native reviews force fresh; same-child resume retains own history. One current outbound-window snapshot, sibling isolation, no archive resurrection/count cap/silent fallback, reference-only portable text and completed tool groups; no parent hierarchy/native replay/permission inheritance. Unsupported media or opaque native checkpoints refuse explicitly.
- **Origin/prior art:** user-approved local role/default policy. Upstream proposal [#91252](https://github.com/NousResearch/hermes-agent/pull/91252) inspected while open at `7e60ebc5d495efa1fba8c25201727c54792ca3cb`; no code adopted. Its transcript-based capped history/fresh fallback does not meet this contract. Retire when released upstream satisfies the complete boundaries, not merely when fork syntax exists.
- **Verify:** `tests/tools/test_delegation_context_forks.py`, `tests/agent/test_delegation_context_fork_real_loop.py`, existing custom-role, config-schema, review-policy and real SQLite resume suites through `scripts/run_tests.sh`. HTTP tests use a deterministic local SDK transport, not a paid provider.
- **Rollback/activation:** revert the scoped source change; remove configured context_mode fields before running an older parser. No persisted history rewrite. Landing does not activate an existing gateway; update/restart remains parent-owned and outside this source task.

- Display-reference allocation and exact-attempt result-release merging begin SQLite write transactions before eligibility reads, fencing other processes. Known owner-abandoned `unknown` outcomes enter dropped history only with matching terminal event/result evidence and all existing unlabelled/exhaustion guards. Arbitrary unknown lifecycle data stays retained. Regressions: `tests/tools/test_async_metadata_transactions.py`, `test_async_completion_retention.py`.

- Fully recovery-exhausted, positively terminal and provably unlabelled notifications settle as dropped, never delivered, under existing seven-day/count history retention. Card obligations, unknown/malformed records, held claims and unused recovery budgets remain retained. Recoverable work remains intentionally uncapped. Regression: `tests/tools/test_async_completion_retention.py`. This does not promise a global database-size bound or recover already expired history after rollback.

- Durable primary, fallback and MoA launch metadata excludes opaque request overrides, retaining known numeric limits and complete authority fingerprints. Actual requests retain the original values, and authorized legacy resume sanitizes the next snapshot without weakening exact authority checks. Regression: `tests/agent/test_delegation_frozen_runtime.py`. Historical rows are not rewritten by this source change.

- Settled interrupted child checkpoints persist the active frozen route and refreshed nonsecret account identity even when unresolved effects prohibit continuation. Reconciliation and explicit recovery authorization remain mandatory. `tests/tools/test_interrupted_checkpoint_route.py` verifies reopened SQLite state and process-receipt recovery. Rollback must not mistake the older route for the checkpoint's actual execution identity.

- Failed child construction closes allocated agents and releases their SessionDB references through the existing teardown owner, including post-constructor setup, partial sibling batches and pre-run batch handoff. Successful children remain owned by the runner. `tests/tools/test_delegate_allocation_cleanup.py` verifies real allocation/reference counts and both failed and successful ownership transfers. Rollback must preserve active-run and resume-claim ownership.

- Use upstream's canonical `SCHEMA_SQL` reconciliation for async delegations, including the fork's recovery and presentation metadata and counter tables. Retain stable display references and exact ownership.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-016 | Active |
| HERMES-108 | Active |
| HERMES-112 | Active |
| HERMES-124 | Active |

## Patch records

### HERMES-016 — Preserve explicit flat MoA configuration during layered merges

- **Summary:** Detects an explicit legacy flat MoA preset in a user or managed configuration layer and removes only inherited named-preset selectors before the layer is merged. This lets the existing flat-config normalization path select the configured references and aggregator instead of silently using `DEFAULT_CONFIG` models.
- **Surfaces:** `hermes_cli/config.py`; `tests/hermes_cli/test_moa_config.py`.
- **Upstream tracking:** Issue `#82726`. Retire after an upstream release preserves or explicitly rejects flat MoA configuration at the complete `load_config()` boundary instead of silently substituting built-in models.
- **Upstream PR:** None after checked 2026-08-14; issue #82726 only.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_moa_config.py tests/hermes_cli/test_config.py tests/hermes_cli/test_config_loader_e2e.py tests/hermes_cli/test_config_validation.py tests/hermes_cli/test_config_read_guard.py -q` plus an isolated flat-config resolution probe using non-default model identifiers.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later config-loader changes, remove only the flat-MoA regression, and restore affected profiles to named `moa.presets.default` configuration before promotion. Do not return a runtime to flat configuration until released upstream behavior passes the same end-to-end resolution probe.
- **Additional historical subjects (optional provenance):** `fix(config): preserve flat MoA settings during merge`; `docs(maintenance): track flat MoA merge patch`.

### HERMES-108 — Configure Named Custom Subagents

- **Actual-route reconciliation (2026-09-10):** Upstream `6c3d4a4a` made `_swap_credential` return a bool and normalize an Actual-provider `runtime_base` before the route/model refusal check. Both behaviors are adopted: every fork return path now yields `True`/`False` so `agent_runtime_helpers` and `credential_pool` read a real verdict, and Actual routes still force `chat_completions` plus a transport-cache clear. Endpoint normalization is skipped when `_delegation_runtime_pin` is set, because a frozen delegation route must keep its exact launch spelling or strict pin validation rejects an authorized swap. Fork-only ordering: the pin resolves `runtime_base` first, then upstream's refusal check runs against the resulting `stripped_base`, so a refused swap still mutates nothing.

- **Account-rotation correction (2026-09-07):** A named worker hit `usage_limit_reached` because its constructor discarded the parent pool and its request pin froze the launch credential. Preserve an already authorized same-route parent pool only when it owns the launch credential. Fixed credentials and unrelated routes remain fixed. Authorize credential-digest replacement only at the existing pool-swap boundary after verifying pool membership and the unchanged endpoint/provider. No global auth re-resolution or model fallback is added. The new real-conversation regression fails on the base with `completed=False`; unauthorized swaps are rejected before client mutation. Fork-only defect, no upstream filing. Stable subject: `fix(delegate): preserve named worker account rotation (#80)`. Added surfaces: `agent/client_lifecycle.py`, `tests/agent/test_named_subagent_rotation.py`. Regression: `scripts/run_tests.sh tests/agent/test_named_subagent_rotation.py tests/agent/test_custom_subagent_runtime.py tests/test_subagent_audit_recommendations.py`. The trusted history baseline advances to exact reviewed fork commit `f66e6d2e504d0d5448bbe227c1766a8bf44a841b` because that squash commit could not register its own final GitHub-generated ` (#80)` suffix; validation remains fail-closed for every later fork commit. Roll back only this correction commit to restore frozen-account behavior. Retire with HERMES-108 when upstream preserves both route pins and authorized pool recovery.

- **Independent wiring hypothesis (2026-09-06):** The delegated-child constructor passes `memory_access_mode="read_only"`, and `init_agent` implements it, but the public `AIAgent` facade rejects the keyword. Restore the explicitly typed facade parameter, preserving its `None` default and existing forwarding. Prove named construction through the real child builder retains read-only knowledge and disables background review, while unnamed construction remains unchanged. Do not drop the argument, widen constructor kwargs, change credentials, or add configuration. The fix is source-only and reversible without state migration.
- **Summary:** Extend native delegation with trusted `delegation.subagents` definitions and per-task `subagent_type`, fixed model/provider/effort, read-only shared knowledge, and nonsecret resolution metadata. Ordinary delegation and AutoReview keep their existing configuration and lifecycle.
- **Surfaces:** `tools/custom_subagents.py`, `tools/delegate_tool.py`, `run_agent.py`, `agent/agent_init.py`, `agent/codex_runtime.py`, `agent/delegation_context.py`, `agent/memory_manager.py`, `agent/memory_provider.py`, `plugins/memory/hindsight/__init__.py`, `tools/memory_tool.py`, `tools/skill_manager_tool.py`, `tools/skills_tool.py`, `toolsets.py`, `hermes_cli/setup.py`, `scripts/smoke_custom_subagents.py`, `tests/tools/test_custom_subagents.py`, `tests/agent/test_custom_subagent_runtime.py`, `tests/test_custom_subagent_knowledge.py`, `website/docs/user-guide/features/delegation.md`, and this manifest.
- **Upstream tracking:** Related open [issue #80222](https://github.com/NousResearch/hermes-agent/issues/80222), checked 2026-09-04. This implementation intentionally exposes named trusted definitions, not arbitrary model-callable route overrides.
- **Upstream PR:** Related [#83343](https://github.com/NousResearch/hermes-agent/pull/83343) is open at `bf48118f5259e08d8e58ed26e91be7179597233b`, checked 2026-09-04. Its filesystem personas and capability narrowing do not implement this registry, subscription-route lock, or shared read-only knowledge contract. Related #4929, #18522, and #53531 are closed without merge. No direct upstream PR. The relevant #83343 feedback on repeated discovery and global mutable warning caches is avoided by one validated batch snapshot and no warning cache.
- **Regression:** `scripts/run_tests.sh -j 4 tests/tools/test_custom_subagents.py tests/agent/test_custom_subagent_runtime.py tests/test_custom_subagent_knowledge.py tests/tools/test_delegate*.py tests/tools/test_async_delegation*.py tests/agent/test_run_agent_codex_responses.py -q`. Covers definition validation, whole-batch launch preflight, legacy delegation, concurrent efforts, correction/retry/iteration-summary paths, physical SDK route guards, cancellation, parent isolation, and parent-owned knowledge writes. `python scripts/smoke_custom_subagents.py` exercises actual Luna/Terra medium subscription calls, skill/standing-memory/session fixture attribution, a worker artifact and successful native command, and an authorization blocker. Full local CI and independent AutoReview receipts belong to the landing PR. Live smoke evidence must include `physical_request: true` entries for both exact models, not only request-builder entries. Read-only provider initialization failures and missing contracts are reported in `unavailable_memory_providers`; embedded Hindsight startup remains disabled and documented. The smoke is isolated, not production-bank or live-profile activation evidence.
- **Published commit identity:** Expected stable subjects `feat(delegate): add named custom subagents` and `fix(review): stabilize exact candidate contracts`; the latter preserves the schema-size ceiling while retaining every unique top-level routing and safety keyword.
- **Rollback:** Revert only `feat(delegate): add named custom subagents` and remove its configuration entries during a separately authorized deployment. Preserve legacy delegation defaults, the Astra parent, and AutoReview policy. Source landing does not activate any profile.
- **Retirement:** Retire after released upstream provides equivalent trusted named selection, whole-batch preflight, subscription-account isolation, every child request's effort guarantee, read-only authorized shared knowledge, stable prompt prefixes, and native lifecycle behavior, with the same regression contract passing.
- **Additional historical subjects (optional provenance):** `fix(hermes): restore runtime callsite wiring`.

### HERMES-112 — Close named subagent audit findings

- **Summary:** Twelve findings from the named-subagent audit. Parent-owned memory and skills are now denied through `write_file`, `patch` (including both V4A `Move` endpoints), `terminal`, and `execute_code`, not only the specialized tools, with the residual shell limit stated rather than implied. Unreadable delegation configuration refuses to spawn instead of silently serving legacy defaults; an invalid role is named while `list`/`steer`/`stop` keep working. Named routes are validated at the final physical request for the OpenAI and Anthropic wires, and routes with no such boundary are rejected at launch rather than granted a guarantee that would not hold. `delegation.subagents` is registered and field-validated in the configuration system, batch telemetry records each child's own route, and the tool schema advertises each role's purpose with its fixed model and effort plus selection precedence.
- **Surfaces:** `tools/knowledge_boundary.py`, `tools/custom_subagents.py`, `tools/delegate_tool.py`, `tools/delegation_live_log.py`, `tools/file_tools.py`, `tools/terminal_tool.py`, `tools/code_execution_tool.py`, `agent/chat_completion_helpers.py`, `hermes_cli/config.py`, `hermes_cli/config_defaults.py`, `scripts/smoke_custom_subagents.py`, `scripts/eval_delegation_selection.py`, `tests/test_subagent_audit_recommendations.py`, `tests/agent/test_custom_subagent_runtime.py`, `website/docs/user-guide/features/delegation.md`, and this manifest.
- **Upstream tracking:** Extends HERMES-108. No new upstream issue; the audited behavior is fork-only surface introduced by that record.
- **Upstream PR:** None. The gaps are specific to this fork's registry, subscription-route lock, and read-only knowledge contract, none of which exist upstream.
- **Regression:** `scripts/run_tests.sh tests/test_subagent_audit_recommendations.py tests/tools/test_custom_subagents.py tests/agent/test_custom_subagent_runtime.py tests/test_custom_subagent_knowledge.py -q` covers every mediated write-path bypass against real fixture files, loader failure versus absent configuration, invalid-definition diagnostics with control actions intact, post-middleware request tampering, unpinnable-route rejection, mixed-batch routing telemetry, config-key validation kept in lockstep with the runtime field set, and role visibility through a real `get_definitions()` rebuild. `python scripts/smoke_custom_subagents.py --mode fixture` is deterministic and offline; `--mode active` uses Hermes's own credential resolver (never `~/.codex/auth.json`) against the active roles and records nonsecret credential-source and route evidence. `python scripts/eval_delegation_selection.py` scores the parent's own decisions — delegate or not, role choice, brief sufficiency, fan-out, and retained authorization — against a fixture project, and reports a turn that failed to complete as inconclusive rather than as a pass.
- **Published commit identity:** Expected stable subject `fix(delegate): close named subagent audit findings`.
- **Rollback:** Revert only this stable-subject commit. Nothing here changes credentials, schema, or persisted state; reverting restores HERMES-108 behavior including its unguarded generic write paths. Source landing does not activate any profile.
- **Retirement:** Retire together with HERMES-108, or earlier if released upstream supplies an equivalent enforced knowledge boundary, loader-failure contract, final-request pinning, and role disclosure with these regressions passing.

### HERMES-124 — Frozen custom-subagent fallback, MoA, and resume runtime

- **Fallback identity boundary:** `validate_fallback_identities` rejects duplicate resolved fallback provider/model identities before launch. A named custom primary keeps its selector (for example `custom:primary`) on the actual child, while resolved fallback routes use `custom`. The configured preflight, real child and request-builder path preserves this distinction and validates primary and fallback requests. Constructing an ambiguous `RuntimePin` directly bypasses those admission rules and is not a supported launch path.

- **September 14 resume correction:** A saved MoA child must still belong to a current effective MoA role, and any explicit current `moa_presets` list must include its stored preset. Changing only the default preset does not replace or revoke an existing frozen snapshot. Physical execution routes and credentials remain restored from that snapshot. Regression coverage includes revocation and preserved default-only edits through the saved-child resume boundary.

- **Pinned Codex fallback repair (2026-09-08):** The real SDK appends one slash to the frozen Codex endpoint; copying that spelling onto the child caused strict runtime-pin validation to reject an authorized fallback. At named Codex activation only, retain the configured spelling after requiring the actual client endpoint to be exactly that spelling or its single SDK-added slash. Route, model, mode, effort, and credential guards remain unchanged. No broad URL normalization. Reproduced red through real `AIAgent._try_activate_fallback`, resolver, and SDK construction; `scripts/run_tests.sh tests/agent/test_named_fallback_route.py` covers successful request construction and rejects endpoint/provider/model/mode/credential drift. Source boundary: `agent/chat_completion_helpers.py`; test: `tests/agent/test_named_fallback_route.py`. Independent hypothesis precedes the fix; upstream discussion search skipped because the pin is a fork-only overlay absent from upstream `tools/custom_subagents.py`. Roll back only the named-Codex endpoint-spelling block and its focused test, leaving other HERMES-124 contracts intact. No runtime/configuration changes; live provider acceptance remains separate.

- **Model-preset retirement track (2026-09-10):** Static named-route expansion now feeds fork named roles and their frozen MoA/reference/aggregator fallback snapshots without altering dynamic routing or aliases. Track replacement at [upstream #107745](https://github.com/NousResearch/hermes-agent/issues/107745) and [PR #107760](https://github.com/NousResearch/hermes-agent/pull/107760); retire this fork adapter only after the released upstream path preserves named-role credential and frozen-resume authority under these regressions.

- **Summary:** Extends named subagents with explicit parent inheritance, fully preflighted ordered availability fallbacks, native MoA preset selection with frozen physical routes, and parent-controlled continuation on the existing durable session lineage. The global 250-iteration limit is a segment boundary: exhaustion is reported as `budget_exhausted`, never completion, and no new or unlimited segment starts automatically. Persisted launch metadata is versioned and nonsecret; resume fail-closes without consuming its grant on foreign lineage/profile, role or route drift, active turn leases, and unmatched persisted tool calls whose external effects are unresolved.
- **Surfaces:** `tools/custom_subagents.py`, `tools/delegate_tool.py`, `tools/delegate_tool_child_run.py`, `tools/delegate_tool_dispatch.py`, `tools/process_registry_notifications.py`, `hermes_state_sessions.py`, `agent/moa_loop.py`, `agent/chat_completion_helpers.py`, `agent/turn_recovery.py`, `agent/turn_facade_lease.py`, `website/docs/user-guide/features/delegation.md`, `tests/tools/test_custom_subagents.py`, `tests/tools/test_delegate.py`, `tests/agent/test_custom_subagent_runtime.py`, `tests/agent/test_delegation_frozen_runtime.py`.
- **Upstream tracking:** Extends fork-only HERMES-108/HERMES-112; no direct upstream equivalent was identified.
- **Upstream PR:** None. This extends the fork-specific named-role contract.
- **Retirement:** Remove this extension when released upstream provides equivalent named-role inheritance, authorized fallback routing, frozen MoA presets, and isolated durable child follow-ups with the same acceptance tests passing.
- **Regression:** Run `scripts/run_tests.sh tests/tools/test_custom_subagents.py tests/tools/test_delegate.py tests/agent/test_custom_subagent_runtime.py tests/agent/test_delegation_frozen_runtime.py tests/agent/test_moa_reasoning_effort.py tests/agent/test_moa_fanout_cadence.py tests/agent/test_turn_facade_lease.py` plus the full suite before publication.
- **Rollback:** Revert this extension without changing live profile configuration. Existing unnamed delegation and the original explorer/worker roles remain the compatibility baseline.
- **Additional historical subjects (optional provenance):** `feat(delegate): freeze named subagent fallback, MoA, and resume runtime`; `fix(delegate): preserve pinned Codex fallback endpoint spelling`.
