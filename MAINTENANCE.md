# Maintained Hermes fork

## Validated stable pointer

[Stable promotion policy](website/docs/developer-guide/stable-promotion.md) owns
semi-stable eligibility and operational evidence. Main continues syncing upstream;
stable is only a validated ancestor pointer. The existing pinned updater freezes
stable once, requires a state snapshot, retains the previous source ref and
manifest evidence, and refuses implicit downgrade. Independent review and 24-hour
observed settling are separate from tests; urgent approval skips age only.
Regression: `tests/hermes_cli/test_update_stable.py` plus the existing immutable
revision suites. Configuration/activation is parent-owned and must wait for a
qualified remote stable ref. No new schedule or runtime activation is included.


## Background

Canonical target: `/Users/brianle/Repos/hermes-agent`, the Brian-owned `0xble/hermes-agent` fork of `NousResearch/hermes-agent`. Upstream remains authoritative for unmodified code; intentional divergence and released-equivalence decisions justify enrollment. Fork `main` is the last candidate that passed fork verification, not evidence of runtime promotion. This root is the sole enrollment and scheduling unit; `maintenance/*.md` extends it without independent enrollment.

The accepted upstream reconciliation cutoffs remain **September 13, 2026** `ee4452991d17534aa561f31ee55596d082aa94e7`, **September 12, 2026** `f364c19775617acb6c16140b790cda9fb05e1906`, and **September 11, 2026** `939e45c91d751fadd94dcd1b873ac3cb44846213`. Immutable source-helper evidence owns each run's test/review/publication status; these observations do not authorize runtime activation. The September 12 integration covered 177 commits from the September 11 cutoff.

Historical accepted history boundaries: Brian approved `1724c48b12a90eb8a76e91e6337eaf2e2965a8e8` on **2026-09-06**; the earlier reconciled boundary was `93455e0d6b40305ac2c9486a06e701d9aef205ca`. They are provenance only: superseded same-commit/duplicate-subject validation claims do not reinstate a gate.

## Preserve and adopt

- The inventory is advisory: publication, PR validation, promotion and operation never depend on commit subjects, trailers or per-commit registration. Missing/stale records require follow-up, not history reconstruction. Source refs and resolved SHAs are optional attribution, never manifest keys.
- Released upstream may replace a private patch only after its complete behavioral contract passes; remove the redundant private implementation, shims, dead code and duplicate fork tests. A related issue, open/merged PR, clean revert or matching subject is not equivalence. Partial coverage leaves an explicit narrower private gap. Retired records are non-resurrection guards; Git owns the discarded chronology and rollback history.
- Every upstream reconciliation inventories live installed/enabled plugins across maintained profiles and compares their actual problem contracts with released native behavior. A full native replacement requires removing the overlapping plugin, its unused Brian-owned canonical source, configuration, dependencies, schedules, skills and generated copies from every affected profile during separately authorized promotion. Disabled duplicates are not retirement; partial overlap keeps only the residual gap. Source completion reports required runtime-only retirement separately.
- `main` advances only through exact-candidate publication with remote-head fencing (guarded PR merge or explicit lease). Automation never pushes to `NousResearch/hermes-agent`. Candidate verification never mutates Personal, LPG or Meridian runtimes. Runtime promotion has its own backup, canary and rollback proof; a runtime rollback neither rewrites the fork nor changes another runtime. Source reconciliation itself does not deploy, restart, or uninstall installed plugins.
- Shared effective-config loading retains authored-layer preset expansion before defaults/managed route merging, including gateway, TUI, cron, and last-known-good recovery. Upstream's presence-sensitive readers still do not inject defaults.
- Adopt upstream manual-compression and native-approval interfaces without losing live-session dedup reset, reconnect-safe Telegram approval prompts, receipt-owned progress or generation-fenced cancellation.
- Adopt upstream test/document relocation; fork-only tests remain under `tests/agent` and `tests/hermes_state`, with canonical import references. Upstream's removed GitHub fallback remains removed under the fork's trusted CI policy.
- Keep MoA frozen physical routes, Telegram Business routing, update reason isolation, and the HERMES-137 flood budget across upstream profile-aware interfaces. Test stubs follow the new profile argument/return shapes without weakening their assertions.
- Preserve exact per-child credentials and reasoning, task-relative knowledge boundaries, verified Hindsight writes, and full redaction before display clipping. Manual supply-chain scans require real immutable endpoints and must fail on unavailable comparison evidence.

## Required responsibility checks

Read and assess **every** linked responsibility on every maintenance run, including a no-change run. The split is justified by independently retireable ownership boundaries across the active patch register, not by line-count buckets. Each module keeps its own prerequisites, invariants, update decision and proof; numbered patch IDs remain stable attribution, not ordering or authorization.

- **Required every run:** [Goals and genuine-user turn authority](maintenance/goals-and-turn-authority.md).
- **Required every run:** [Delegation admission, frozen routes and recovery](maintenance/delegation-runtime.md).
- **Required every run:** [Delegation presentation and receipt ownership](maintenance/delegation-presentation.md).
- **Required every run:** [Managed browser identity and authentication](maintenance/browser-identities.md).
- **Required every run:** [Credential isolation and inference routing](maintenance/credentials-and-routing.md).
- **Required every run:** [Gateway delivery, receipts and chat budgets](maintenance/gateway-delivery.md).
- **Required every run:** [Telegram payload projection and rendering](maintenance/telegram-rendering.md).
- **Required every run:** [Session identity, titles and command context](maintenance/session-context.md).
- **Required every run:** [Memory and skill source ownership](maintenance/memory-and-skills.md).
- **Required every run:** [SQLite state, backup and recovery evidence](maintenance/state-and-recovery.md).
- **Required every run:** [Runtime lifecycle and restart ownership](maintenance/runtime-lifecycle.md).
- **Required every run:** [Cron scheduling, route selection and outbound authority](maintenance/cron-execution.md).
- **Required every run:** [Compression outcome and retry lifecycle](maintenance/compression.md).
- **Required every run:** [Review and local execution boundaries](maintenance/review-and-execution.md).

## Update and overall proof

- Synchronize the maintained default branch with the latest upstream default, preserving intentional behavior and the source/run checkpoint. Freeze the run's upstream cutoff, integrate before final tests and independent review, and stop source writers before that exact-candidate proof. Interrupted effects remain unknown until reconciled. New drift is a separate observation, not permission to silently alter a frozen run.
- On every run and before publication, promotion or retirement, resolve each linked issue, PR, commit and released descendant live: reviews, threads, comments, CI, merges/reverts and releases matter even when source SHAs did not change. Separate direct contribution, adopted source and merely related associations; an issue or bare commit is not a PR. Maintain distinct `Upstream tracking` and `Upstream PR` evidence, including a dated `None` when checked. Never infer upstream acceptance or current runtime status from a source-candidate label or old test count.
- Judge substantive feedback against the actual contract, reproduction and tests; classify valid, invalid, stale, addressed or decision-required with evidence. Apply valid corrections and focused tests to the fork, then synchronize the same verified shared behavior to an authorized associated contribution and read back both heads. Unresolved valid feedback, stale/ambiguous associations or shared-behavior divergence block truthful candidate publication; this is substantive proof, not a subject/registration gate. No association grants upstream write authority.
- Maintain the owning module when a patch materially changes, narrows, retires or changes upstream disposition; do not append a second historical narrative or reconstruct old commits to repair inventory. Run `python3 scripts/validate_maintenance_manifest.py MAINTENANCE.md --json` over the root and its complete required module closure. Findings and unreadable/missing support remain explicitly advisory and exit zero; a passed invocation proves only structural inventory coverage, never behavior or publication eligibility. Discovery still enrolls only this root.
- For the exact integrated candidate, use `scripts/run_tests.sh` for the native Python regression surfaces named by all affected active contracts, even when dated evidence quotes older direct invocations; never install or update dependencies merely to replay a historical receipt. Read coupled dependencies before narrowing or reverting shared behavior, including rollback/retirement and platform limitations. Verify every retirement against released upstream and satisfy repository-owned candidate review. Unchanged patches still need live association/equivalence assessment. Record unavailable evidence honestly; neither a partial module read nor historical pass counts establishes a complete no-op.
- Before reporting `Updated` or `Already current`, fetch upstream again and prove zero upstream-only commits with `git rev-list --left-right --count <upstream-default>...<maintained-default>`; read back owned-remote/default SHA parity. If upstream moved, reconcile/reverify or report `Blocked` with the concrete conflict, failed proof or unavailable authority. Source publication, separately authorized runtime activation, and runtime-only retirement are distinct outcomes.
