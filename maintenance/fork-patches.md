# Fork patch provenance

Read on every maintenance run and whenever a fork patch changes. This support file
belongs to [the root contract](../MAINTENANCE.md), which owns baseline selection
and publication. The register preserves the pre-contract commit classifications
because `scripts/check_fork_patches.py` uses them to recognize older untrailered
commits. Evidence-only rows are compatibility records, not additional patches.

## Update and proof

Compare each retained behavior with the selected upstream release before replaying
its commits. Keep source attribution and retirement conditions when adapting a
patch. New commits need `Fork-Patch:` trailers and material behavior changes need
an updated record here. A trailer proves classification, not functional coverage.
Use the repository test runner on the affected surfaces below and verify that the
ledger check recognizes the full series. Retire a patch only after its regression
passes on the selected upstream release without the local implementation.

| Responsibility | Source / proof surface | Contribution and current limits |
|---|---|---|
| Goal, review, memory journal, update request | `candidate-extensions/` and its tests; plugin schema/discovery tests | Personal plugins, retained locally. Review completion must bind to the dispatched child; request-update uses native spawn/watch. |
| Canonical skills | `plugins/canonical-skill-guard/`, `scripts/curate_skill_observations.py` | Personal ownership policy. Curation publication must verify PR success before treating observations as processed. |
| Camofox accounts and vault | browser account/vault tests and `tests/agent/test_vault_connect.py` | Account aliases are profile-specific. Adopted source: [upstream PR 114414](https://github.com/NousResearch/hermes-agent/pull/114414), open at `d8a374630aef825ad3d86c1e41defa57a4874247` on 2026-09-19. Fork adaptation is delivered; upstream merge is separate. |
| Telegram emphasis | `tests/gateway/test_telegram_emphasis.py` | Own contribution: [upstream PR 106906](https://github.com/NousResearch/hermes-agent/pull/106906), open at `37f872bad1706c6c50ccdccb825fc4d5ffd2c246` on 2026-09-19. |
| Telegram paragraph spacing | `tests/gateway/test_telegram_rich_newlines.py` | Own contribution: [upstream PR 100686](https://github.com/NousResearch/hermes-agent/pull/100686) for [issue 100664](https://github.com/NousResearch/hermes-agent/issues/100664), open at `c90504124b06c12b24ba9c9d0dca6a3ca479a764` on 2026-09-19. Rich Message prose paragraph breaks become one NBSP spacer row; structural blocks stay raw. Contract in [Telegram rendering](telegram-rendering.md). |
| Telegram flood recovery | `tests/gateway/test_telegram_flood_coherence.py`, split-send and delivery-ledger tests | Adopted commits are pinned below. Media rate limits are classified but attachments are not durable redelivery obligations. |
| Cron | per-job-timezone and contention-skip tests under `tests/cron/` | Local narrow patches pending equivalent released behavior. Preserve civil-time scheduling and truthful skip results. |
| Backup and state | `tests/hermes_cli/test_backup.py`, `scripts/schema_rehearsal.py` | Adopted backup fixes plus local integrity/rehearsal tooling. Verify on copies, including fork-only rows. |
| Maintenance tooling | `scripts/{sync_fork_candidate,check_fork_patches,install_candidate_extensions}.py`, `scripts/rollback_fork_runtime.sh` | Fork-specific tooling, not an upstream runtime feature. Exercise rollback failure recovery and native update receipt checks before promotion. |

For rows without a linked contribution, no upstream submission is recorded here.
Reassess the contribution route when changing that behavior rather than treating
absence of a submission as evidence that upstream lacks it. Source rollback is a
reviewed revert of the affected logical patch and dependent adaptations; installed
runtime rollback follows the separate runtime owner.

For cron-specific fallback divergence, load [cron fallback routing](cron-fallback-routing.md).

## Classified commits

| commit | subject | slice | upstream | retire when |
|---|---|---|---|---|
| `664eeb3baabd` | docs: record release candidate baseline | evidence | none | not a patch; evidence/config record |
| `ba626698191d` | docs: record legacy migration references | evidence | none | not a patch; evidence/config record |
| `22a5a538246c` | feat: add isolated slice 2 routing profile | evidence | none | not a patch; evidence/config record |
| `d1a7878e05d6` | feat: add restricted goal lifecycle plugin | slice-3-goal-lifecycle | none | when upstream ships an agent-callable goal tool with a restricted action set |
| `63f94b2ae9b3` | docs: record goal lifecycle evidence | evidence | none | not a patch; evidence/config record |
| `d49a7f310f04` | feat: add candidate-bound review plugin | slice-4-review-gate | none | when upstream /review accepts a ref and records a receipt |
| `56fab940b0d9` | fix: reuse matching candidate review receipts | slice-4-review-gate | none | when upstream /review accepts a ref and records a receipt |
| `2653759101a8` | feat: add hash-chained memory journal | slice-5-memory-journal | none | when upstream adds a memory ledger comparable to skill_ledger.py |
| `4054a6a73aa9` | test: exercise memory journal through built-in tool | slice-5-memory-journal | none | when upstream adds a memory ledger comparable to skill_ledger.py |
| `c92231625645` | fix: preserve Telegram nested emphasis | slice-11-telegram-emphasis | NousResearch/hermes-agent#106906 | when the emphasis fix merges upstream and the candidate tag includes it |
| `6d722d42aed3` | docs: record Telegram emphasis verification | evidence | none | not a patch; evidence/config record |
| `f500063ab41a` | fix(cron): persist contention skip observability | slice-12 truthful-contention | none | when upstream records contention skips |
| `291fb7bfbf1c` | fix(backup): report incomplete archives as failures | slice-13-incomplete-archive-status | adopted from upstream commit ccb3d968ced | when retained by candidate upstream |
| `3880aa94f0dd` | fix(backup): preserve complete archives during retention | slice-13-bounded-retention | adopted from upstream commit 1250a3e4eb6 | when retained by candidate upstream |
| `0c74190b15e7` | fix(backup): verify SQLite snapshot members | slice-13-snapshot-integrity | none | when upstream verifies quick-snapshot recovery copies |
| `9e178344f1a2` | feat(update): add parent-only native update request | slice-14-request-update | none | when upstream exposes an agent-callable update trigger |
| `437efe5d0a9b` | docs: record backup and update slice evidence | evidence | none | not a patch; evidence/config record |
| `0209d04eb47a` | docs: record candidate remote publication | evidence | none | not a patch; evidence/config record |
| `3123777f4d84` | fix(telegram): resume split sends after flood refusals | slice-10-telegram-delivery | adopted from upstream commit 595f3a289c7 | when candidate upstream contains split-send recovery |
| `c65ac8f9e9e6` | fix(gateway): suppress duplicate fallback after partial delivery | slice-10-telegram-delivery-followup | adopted from upstream commit 969898d4ff4 | when candidate upstream contains partial-delivery suppression |
| `c1c37ad01460` | fix(gateway): back off rejected deliveries and preserve recovery | slice-10-delivery-ledger | adopted from upstream commits c961e5bb691 and 807435ac1ec | when candidate upstream contains failed-row redelivery backoff |
| `7e9ca86ff36a` | docs: record Telegram delivery evidence | evidence | none | not a patch; evidence/config record |
| `1d89847ca870` | docs: record broad Telegram verification | evidence | none | not a patch; evidence/config record |
| `5c123acb9dba` | feat(browser): route named camofox accounts | slice-8-camofox-accounts | none | when upstream ships named account selection |
| `a0d34d6cccc2` | fix(vault): support Connect and secret-safe Camofox login fills | slice-9-vault-camofox | NousResearch/hermes-agent#114414 | when #114414 or equivalent merges upstream and the candidate tag includes it |
| `dbfdd2c1864a` | fix(vault): mint TOTP codes from Connect one-time-password fields | slice-9-vault-camofox | NousResearch/hermes-agent#114414 | when #114414 or equivalent merges upstream and the candidate tag includes it |
| `55c38d2dbbdf` | test(vault): make the Connect TOTP regression clock-boundary safe | slice-9-vault-camofox | NousResearch/hermes-agent#114414 | when #114414 or equivalent merges upstream and the candidate tag includes it |
| `e1c3bcdeccb0` | fix(vault): only announce Connect automatic 2FA when a code can really be minted | slice-9-vault-camofox | NousResearch/hermes-agent#114414 | when #114414 or equivalent merges upstream and the candidate tag includes it |
| `146cc02e7635` | fix(vault): do not let an unusable Connect OTP field hide a usable one | slice-9-vault-camofox | NousResearch/hermes-agent#114414 | when #114414 or equivalent merges upstream and the candidate tag includes it |
| `1875297d660f` | fix(vault): preserve upstream multi-origin metadata for Connect | slice-9-vault-camofox | NousResearch/hermes-agent#114414 | when #114414 or equivalent merges upstream and the candidate tag includes it |
| `21dfc1e1a7fc` | fix(vault): adapt multi-origin metadata to candidate | slice-9-vault-camofox | NousResearch/hermes-agent#114414 | when #114414 or equivalent merges upstream and the candidate tag includes it |
| `214430392ef6` | feat(skills): guard canonical external skill writes | slice-6-skill-guard | none | when upstream adds an external-dir-aware write guard |
| `e9830642caa5` | test(hindsight): verify disposable profile bank isolation | slice-7-hindsight | none | not a customization; profile bank evidence |
| `5548fa5e9530` | config(candidate): enable canonical skill guard | evidence | none | not a patch; evidence/config record |
| `4ccee233499b` | docs(candidate): record skill guard activation | evidence | none | not a patch; evidence/config record |
| `8a04c24f00c8` | docs(candidate): deduplicate slice baseline notes | evidence | none | not a patch; evidence/config record |
| `2ee7639820de` | docs(hindsight): record optional client verification | evidence | none | not a patch; evidence/config record |
| `27febce31ed8` | feat(candidate): install extensions through real plugin discovery | candidate-tooling | none | when the extensions ship as packaged plugins |
| `f9b216c151e6` | fix(candidate): repair extension schemas and update watcher | slice-3-goal-lifecycle | none | when upstream ships an agent-callable goal tool with a restricted action set |
| `2499dec11f8d` | fix(candidate): bind review receipts to one candidate and one reviewer | slice-4-review-gate | none | when upstream /review accepts a ref and records a receipt |
| `55f44502a28e` | fix(browser): scope Camofox account aliases to the active profile | slice-8-camofox-accounts | none | when upstream ships named account selection |
| `567d65c47276` | fix(telegram): one flood window across edits, typing, and uploads | slice-10-flood-coherence | none | when upstream classifies media floods and shares one per-chat window |
| `d13f56a05ca6` | docs(candidate): record post-review repairs and corrected slice 10 status | evidence | none | not a patch; evidence/config record |
| `c5e878dd1d23` | docs(candidate): state why media ledger obligations need a lifetime decision | evidence | none | not a patch; evidence/config record |
| `f0d2799b42a2` | fix(candidate): parse the reviewer's verdict into the receipt; record live proofs | slice-4-review-gate | none | when upstream /review accepts a ref and records a receipt |
| `ee900244de91` | feat(cron): per-job IANA timezone and a migration dry-run diff | slice-12-per-job-timezone | none | when upstream adds a per-job timezone |
| `d076883f6aa1` | fix(candidate): run candidate reviews in the background, receipt on child stop | slice-4-review-gate | none | when upstream /review accepts a ref and records a receipt |
| `53f9d4be8094` | feat(candidate): verify the memory journal chain and bound its growth | slice-5-memory-journal | none | when upstream adds a memory ledger comparable to skill_ledger.py |
| `087a66723ba7` | feat(candidate): fork patch ledger, sync stage, feature check, and rollback | slice-14-self-update | none | when the fork carries zero patches |
| `474aea279b9b` | feat(candidate): schema-compatibility rehearsal for copied legacy state | slice-13-schema-rehearsal | none | never (migration tooling) |
| `f85ebdeadd8a` | feat(candidate): skill curation stage and the candidate's scheduled jobs | slice-6-skill-curation | none | if upstream adds an observation channel and an external-dir-aware curator |
| `557efe2ea9af` | feat(candidate): install cron scripts with the extensions; company overlay diff | candidate-tooling | none | when the extensions ship as packaged plugins |
| `296aea1844d9` | docs(candidate): personal cutover runbook; Meridian lineage settled by ancestry | evidence | none | not a patch |
| `8ca0e1a627da` | feat(candidate): verified archive of the legacy fork's complete history | slice-18-archive | none | never (migration tooling) |
| `5a39c6b954f8` | fix(candidate): address all seven I6 review findings | slice-4-review-gate | none | when upstream /review accepts a ref and records a receipt |
| `42308cb106bf` | docs(candidate): record the I6 review, its disposition, and the corrected Hindsight claim | evidence | none | not a patch; evidence/config record |
| `91993b5452cd` | fix(candidate): address all nine findings of the second I6 review | candidate-tooling | none | when the extensions ship as packaged plugins |
| `5fc72d8161e9` | docs: record the company overlay decisions taken at cutover | evidence | none | compatibility record, not a runtime patch |
| `7004e57bda8d` | docs(candidate): record the company cutovers and slice 18 retirement | evidence | none | compatibility record, not a runtime patch |
| `517e6561974f` | docs(candidate): record the company acceptance evidence and the quota-gated rows | evidence | none | compatibility record, not a runtime patch |
