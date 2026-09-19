# Fork patch ledger

Every commit on the candidate branch above upstream tag `v2026.9.14` (`345cd2b057a452236de401d3534b8502a7465e8d`),
with the slice it serves, the upstream PR it tracks, and the condition under which it retires.
Commits that landed without a `Fork-Patch:` trailer are classified here from their content;
the trailer is required on every new commit and `scripts/check_fork_patches.py` enforces both.

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
