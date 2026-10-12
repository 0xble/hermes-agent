# One-Shot Task-Governing Skill Loading

## Fork Patch Identity

This maintenance unit owns the fork patch identity `oneshot-task-governing-skill-loading`.

## Contract

A one-shot session keeps the finite-run footprint reduction: `skill_manage` is hidden and skill-maintenance coaching is omitted. It must still load a matching skill when that skill governs task-specific procedures, permissions, required workflow, or user/project conventions, even when the agent already knows the generic process. Unrelated broadly useful process skills may still be skipped.

This preserves the one-shot boundary without making procedural skills invisible to scripted evaluation or task execution.

## Evidence And Disposition

The loop-ownership trigger diagnosis at `/Users/brianle/.hermes/cache/scratch/loop-ownership-trace-diagnosis-3fc37cfa/diagnosis.md` identified the restrictive one-shot guidance in `agent/oneshot_footprint.py` and the interactive matching-skill requirement in `agent/prompt_builder.py`. The smallest adequate change is guidance-only. It does not force-load a named skill, change evaluator checks, or re-enable skill authoring.

At the implementation preflight, the fork was based on `54f27baa0695c9b0f8bf57ba0184944130afb8ba` and upstream `main` resolved to `8a7a5a905fc66ed82b922587a8e3c011e819dbb5`. Upstream PR [#63814](https://github.com/NousResearch/hermes-agent/pull/63814) addresses a separate one-shot `--skills` forwarding defect. The upstream `agent/oneshot_footprint.py` still carries the restrictive knowledge-only wording, and no equivalent upstream change was found in the bounded prior-art search.

## Verification

- `scripts/run_tests.sh tests/agent/test_oneshot_footprint.py tests/agent/test_prompt_builder.py`
- The focused suite passed with `60 tests passed, 0 failed, 1 skipped`.
- `git diff --check` passed.

Behavioral evaluation lift remains unverified until the provider route is healthy and lifecycle receipts preserve `CaseResult.trace`.

## Retirement And Rollback

Retire this patch when upstream's one-shot matching-skill contract loads task-governing procedural skills without restoring one-shot skill authoring or maintenance coaching. Roll back by reverting the commit carrying `Fork-Patch: oneshot-task-governing-skill-loading`.
