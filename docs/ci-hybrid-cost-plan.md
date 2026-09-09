# Hybrid CI cost rollout

## Scope and acceptance

Accepted 2026-09-09 for the private `0xble/hermes-agent` fork. Baseline live main:
`89bf71c0c9bed8b026034d17b78949e2bbb2425b`. Delivery uses the isolated
`ci/hybrid-cost` branch and normal PR landing, not runtime promotion.

- Preserve Python/JS execution coverage, native OS coverage, local CI and independent review.
- Preserve trusted `pull_request_target` policy and stable required status names.
- Retry unavailable/incomplete immutable change comparisons, then block. Never convert
  an API failure into success, mutable-current-PR classification, or an expensive full matrix.
- Use slim only after a real hosted capability pilot. Use a repository-scoped Linux
  worker only after isolation and actual workload execution are verified.
- Gross hosted acceptance budget: $20–22/month across the personal account, before credits.
  This is a target, not a forecast or demonstrated saving.

## Baseline and ownership

The maintained checkout is `/Users/brianle/.hermes/hermes-agent`; no audit tarball is
used for delivery. Its primary worktree and other owners' worktrees are untouched.
Existing PR #95 changes maintenance-policy validation; this rollout does not change
that implementation or its protected phase-1/phase-2 checkpoints. The already-landed
functional CI/advisory-maintenance semantics remain intact.

Evidence inputs (local audit records): `/tmp/github-ci-workflow-audit/audit-summary.md`
and `/tmp/github-ci-audit-20260909/`. September 1–9 reconstructed Hermes usage was
approximately $105.3, with Python the dominant expense. That reconstruction is not an
invoice and blocked runs understate demand.

## Capacity discovery and unresolved worker decision

Read-only discovery found no registered repository runners. Personal Mac Studio has
28 cores and 96 GB RAM, but its Data volume is 94% used with about 121 GiB free.
The sole running Colima profile (`default`) is a 4-vCPU/8-GiB ARM64 Linux VM shared
with personal and LPG/Supabase workloads and writable personal host mounts. **It is
not an acceptable CI worker and must not be reused.** No company host is authorized.

Concrete next option: a separate bounded Colima VM/profile, no host mounts, no SSH
agent forwarding, no shared daemon, no auto-published guest ports, and verified
network isolation from host/LAN/company services. Keep its runner repository-scoped,
ephemeral, and read-only; do not copy personal GitHub or production credentials into
it. Prove ARM64 dependency compatibility (the existing test workflow explicitly
installs x86_64 ripgrep), resource headroom, cleanup and failure behavior before routing.
Disk allocation, host/LAN isolation and ARM64 coverage remain unverified; no runner
labels are introduced merely to make YAML appear complete. A separate already-owned
isolated x86_64 Linux host is the alternative if available. No paid capacity is authorized.

## Staged delivery

1. Fail-closed compare retry boundary and behavioral regressions. Preserve explicit
   dispatch/schedule full coverage and complete empty-compare behavior.
2. Slim capability proof must exercise actual pinned checkout/uv, Python, Git,
   GitHub CLI, immutable compare access, smoke checks and classification.
   Attempted pilot: https://github.com/0xble/hermes-agent/actions/runs/34397381195.
   The job did not start: GitHub reported failed recent account payments or a spending
   limit requiring attention. No steps executed, so there is no runtime/cost proof.
   Independent review also found the trusted base policy only allows the existing
   standard hosted labels. The branch-only pilot workflow was removed from this
   delivery, not exempted from policy. A separately reviewed, normally landed policy
   authorization must precede a new slim/self-hosted workflow; candidate policy cannot
   authorize itself. Routing remains unchanged. Billing is owned separately; this
   change neither raises a limit nor bypasses it.
3. Provision and verify the selected isolated worker; run the actual Python and JS
   workloads and collect duration/queue/failure evidence before moving their routing.
4. Add evidence-backed affected-platform rules with regression coverage while retaining
   periodic full native runs. Do not replace native tests with fake host-platform flags.
5. Measure job-level rounded hosted minutes and rates over the pilot workload; validate
   the monthly envelope rather than infer success from runner labels or included credits.

A failed or unavailable runner must queue/block CI, never pass it. Rollback is a scoped
revert of this rollout's eventual commit(s), without changing billing, other repositories,
maintenance policy, or runtime state.
