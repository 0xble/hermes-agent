# Live gateway inference controls

## Required behavior

When a running gateway turn receives canonical `/reasoning` or `/fast` (including an alias), the accepted session choice reaches that turn's *next* model request. A request already assembled is not rewritten. The running agent and its prompt cache stay intact; no transcript command is injected or turn interrupted. Session/global settings retain their existing persistence semantics, and fast-mode toggles preserve unrelated provider request overrides. Reset restores the effective configured reasoning level, including the active model's override. Idle agents may still be evicted.

## Provenance and patch

Fork patch identity: `gateway-live-inference-controls`.

Adapted from archived HERMES-094 (historical subject `feat(gateway): apply inference controls mid-turn`), whose previous gateway cache and command interfaces differ from the current release fork. On base `c8e3342595ea5021c920ba707e70926d20f1f9f2`, the busy command is dispatched, but `slash_commands_model.py::_set_reasoning_override` and `_apply_fast_selection` update session intent and evict the cached agent without updating the running one. Its second model call retains stale `reasoning_config` and `request_overrides`. The invariant regression failed with `agent.reasoning_config` still `low` after selecting `high`.

Upstream [PR #123803](https://github.com/NousResearch/hermes-agent/pull/123803) contributes the complete busy-dispatch and live-request fix from `456ac3ee13ae6260ce78dc33db4530b4a96f07c4` (open, unreleased). The fork adaptation omits `hermes_cli/commands.py` and `gateway/run_busy.py` edits because those two commands already dispatched on the maintained fork. Historical closed #10116 attempted the busy toggles and merged #12955 excluded inference controls because the handlers did not update the live agent. Open #92187 handles durable session options but explicitly rejects busy mutation.

## Verification

`scripts/run_tests.sh -j 6 tests/gateway/test_running_agent_session_toggles.py tests/gateway/test_fast_command.py tests/gateway/test_reasoning_command.py tests/gateway/test_command_bypass_active_session.py tests/gateway/test_session_race_guard.py tests/gateway/test_busy_command.py -q`.

## Retirement and rollback

Retire when an adopted upstream release both dispatches inference controls while busy and applies them to the next request without evicting the active agent, preserving unrelated overrides and global/reset semantics. Revert the scoped fork patch and its tests and index row; leave `maintenance/gateway-commands.md` and unrelated busy-command dispatch intact. No persisted-state migration or runtime activation is part of this patch.
