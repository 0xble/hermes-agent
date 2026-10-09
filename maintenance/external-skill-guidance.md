# External skill maintenance guidance

## Fork patch identity

This maintenance unit owns the fork patch identity `external-skill-guidance`.

## Behavior

When `skills.external_dirs` is configured, the cached foreground skills prompt tells agents that external skills are read-only installs and must be changed at their source in the owning repository, while preserving `skill_manage` guidance for locally owned skills. Background skill-review prompts carry the same source-maintenance instruction. Autonomous `skill_manage` refusals for external skills also name the owning repository as the change location.

## Source surfaces

- `agent/prompt_builder.py`
- `agent/background_review.py`
- `tools/skill_manager_guards.py`
- `tests/agent/test_prompt_builder.py`
- `tests/agent/test_refine_focus.py`
- `tests/tools/test_skill_manager_tool.py`

## Upstream status

Upstream `main` still emits the unconditional foreground `skill_manage(action='patch')` guidance and does not mention the owning source repository in the background review prompt. Searches found related external-directory discovery and ownership issues, including [#134490](https://github.com/NousResearch/hermes-agent/issues/134490) (opt-out for background-review writes on tracked external trees), but no equivalent prompt-guidance fix. See also [#42378](https://github.com/NousResearch/hermes-agent/issues/42378) for bundled/hub mutation guards.

## Focused regression

`scripts/run_tests.sh tests/agent/test_prompt_builder.py tests/agent/test_background_review.py tests/agent/test_background_review_cache_parity.py tests/agent/test_background_review_memory_scope.py tests/agent/test_background_review_toolset_restriction.py tests/agent/test_refine_focus.py tests/tools/test_skill_manager_tool.py`

## Retirement

Remove this patch when released upstream distinguishes external-dir skill maintenance in both foreground and background guidance and provides equivalent source-repository refusal guidance.

## Rollback

Revert the commit carrying `Fork-Patch: external-skill-guidance`.
