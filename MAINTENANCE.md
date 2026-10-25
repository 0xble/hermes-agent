# Hermes fork maintenance

## Background

Canonical source: `/Users/brianle/Repos/hermes-agent`, published as
[`0xble/hermes-agent`](https://github.com/0xble/hermes-agent), branch `main`.
Upstream is [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent),
default branch `main`, remote `upstream-live`. Accepted release baseline:
`v2026.9.14`, `345cd2b057a452236de401d3534b8502a7465e8d`.
This replacement fork was established on 2026-09-19. The former fork is preserved
as `0xble/hermes-agent-archived`; its history is not the replacement's baseline.

## Preserve

- Maintain `main` as an upstream **release tag plus narrow, justified patches**.
  This is the accepted migration design's exception to default-branch tracking.
  Inspect upstream `main` for fixes, but do not silently adopt it as the baseline.
- Keep profile routing, credentials, Hindsight banks, and browser identities
  separate. Personal plugin and scheduling policy does not override company overlays.
- Keep upstream delegation and update machinery as the owners of their lifecycles.
  Local plugins extend those boundaries rather than restore the retired fork's machinery.
- Source checkout, installed personal checkout, and signed company releases are
  distinct identities. A source merge is not deployment or acceptance evidence.

## Maintenance units

| Unit | Required behavior | Load when | Contract |
|---|---|---|---|
| Loop-cap final answer | Refuse excess searches or subagents, then let the model answer once without executing more tools | Per-turn loop-cap handling, final-answer requests, or upstream guardrail changes | [Loop-cap final answer](maintenance/loop-cap-final-answer.md) |
| Provider-reported cost | Capture a provider's finite per-call billed amount and persist it so every existing reader displays it exactly once, without changing estimated/actual combine semantics | Usage normalization, `estimate_usage_cost`, main-turn cost persistence, or `update_token_counts` cost columns | [Provider-reported cost](maintenance/provider-reported-cost.md) |

## Active patch record: shared primary cooldown

- **Patch identity:** `shared-primary-cooldown`.
- **Behavior:** A primary route that is rate-limited, billing-limited, or upstream-rate-limited writes an atomic, file-locked cooldown under `$HERMES_HOME/state/model_cooldowns.json`. Fresh agents adopt the configured fallback without calling the primary while the shared record is active. One process claims the outage notice; one successful primary response clears the record and emits the recovery notice.
- **Source surfaces:** `agent/shared_primary_cooldown.py`, `agent/fallback_cooldown.py`, `agent/agent_runtime_helpers.py`, `agent/chat_completion_helpers.py`, `agent/chat_completion_nonstream.py`, and `hermes_cli/fallback_cmd.py`.
- **Focused regression:** `scripts/run_tests.sh tests/agent/test_shared_primary_cooldown.py tests/agent/test_provider_fallback.py tests/agent/test_fallback_exhaustion_cooldown.py` plus `evals/provider_fallback/probe_shared_primary_cooldown.py`.
- **Retirement:** Remove when upstream provides equivalent shared cooldown, fresh-agent adoption, notice ownership, recovery clearing, and CLI status/clear controls.
- **Rollback:** Revert the commits carrying `Fork-Patch: shared-primary-cooldown`.

## Update

Every run loads [fork patch provenance](maintenance/fork-patches.md). Its separate
responsibility is retained divergence, source attribution, and patch retirement.
Load [runtime ownership](maintenance/runtime-ownership.md) whenever changing
installation, update/rollback tooling, scheduled procedures, or recovery evidence.
These are support files for this contract, not independently scheduled targets.

Fetch `origin/main` and upstream release tags, select the newest upstream release,
and reconcile each logical patch against it in an isolated worktree. Compare
upstream `main` separately for unreleased fixes worth explicit temporary backports.
Use `scripts/sync_fork_candidate.py` only as a candidate builder: a successful
rebase or published candidate does not authorize promotion. Refresh release-tag
selection before reporting current and prove the selected tag is an ancestor of
the proposed fork head. Report upstream-main divergence separately from release
currency. Preserve this release policy when applying generic maintenance guidance.

## Verify

- Run `scripts/run_tests.sh` for every affected patch's proof surface in the
  provenance register, including plugin discovery when extension code changes.
- If media or browser fixtures fail on this host, check for synthetic `198.18.0.0/15`
  DNS answers before attributing a regression. Preserve the SSRF guard; verify
  the environment cause instead of blanket-skipping failures.
- Run `scripts/check_fork_patches.py` against the intended source/profile pair.
  Its commit classification and registration checks do not establish runtime
  acceptance; verify native update receipts and running identities as well.
- Prove selected-tag ancestry, review the remaining fork diff, and read back the
  published `origin/main` SHA. Record a failed stage instead of reporting current.
- For separately authorized activation, use the runtime support file's host-specific
  acceptance and recovery requirements. Preserve unresolved acceptance gaps until
  demonstrated behavior closes them.

## Deploy-Base Qualification Repairs

Fork patch identities: `lpg-backport-qualification`, `lpg-provider-credentials`.
These repairs are scoped to the LPG deploy base in [PR #790](https://github.com/0xble/hermes-agent/pull/790).
Subprocess output uses explicit UTF-8 decoding, inherited contributor Baris Sencan
is mapped to `isair`, and the complete existing CI runs on standard hosted runners.
The canonical runner partitions the full discovered Python suite into 10 slices.
The existing aggregate rejects cancelled dependencies and is required by `qualification`.
The contributor collision guard verifies preserved exact filenames and content on
case-insensitive filesystems.

models.dev listing a provider must not hide credentials already declared by its
registered plugin. Resolution keeps catalog and plugin key names plus the existing
overlay, without changing endpoint/transport precedence. The existing CommandCode
resolution assertion and `tests/hermes_cli/test_provider_catalog_credentials.py`
guard this. Retire the patch when upstream resolution preserves the same union.

Fork patch identity: `lpg-qualification-fixtures`. The inherited nested-emphasis
renderer fix had not regenerated the Telegram conformance vectors. Regenerate
with `scripts/generate_conformance_vectors.py`, retaining the unchanged corpus
and all parity assertions. The bare Slack adapter test double must initialize
its constructor-owned channel-type cache. Local quickstart sequencing tests use
the existing deterministic preflight fixture, preserving both successful setup
leg assertions and the separate refusal tests. Guards: `tests/conformance/test_vector_generator.py`,
`tests/gateway/test_slack_mention.py`, and `tests/hermes_cli/test_local_quickstart.py`.

The inherited `fallback.min_switch_reset_seconds` setting also needs its
`fallback` category folded into the existing agent tab through `_CATEGORY_MERGE`.
The existing `test_no_single_field_categories` contract remains unchanged.

The concurrent cron/profile isolation fixture must accept `compute_next_run`'s
existing third timezone argument. Only the stub signature changes. Its concurrent
write ordering and both profile-store isolation assertions remain unchanged.
Guard: `tests/hermes_cli/test_web_server_cron_profiles.py`.

JS/TS and bootstrap-installer checks also use standard hosted runners. Their
large-runner labels inherited from upstream are unavailable to this fork. All
existing commands and acceptance checks remain in both jobs.
