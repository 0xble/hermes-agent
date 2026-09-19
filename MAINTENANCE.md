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

| Unit | Purpose | Load when | Contract |
|---|---|---|---|
| Cron fallback routing | Keep scheduled agents' backup chain independent of interactive routing | Changing cron/provider resolution or evaluating an upstream release | [Cron fallback routing](maintenance/cron-fallback-routing.md) |

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
