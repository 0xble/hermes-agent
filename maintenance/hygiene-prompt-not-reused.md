# Hygiene prompt not reused

Load this unit when changing how a continuing session restores its stored system prompt
(`agent/conversation_loop.py::_stored_prompt_matches_runtime`) or how gateway hygiene
compaction rebuilds it.

## Required behavior

A stored prompt whose `Platform:` line is `gateway_hygiene` is treated as stale runtime
identity, so the next real turn rebuilds and persists a full prompt. Other platform changes
still reuse the stored bytes and stage a surface-switch note (#104414).

Fork patch identity: `hygiene-prompt-not-reused`.

## Why

Gateway hygiene compaction runs a memory-only agent, and the compaction boundary rebuilds
the system prompt from that agent. The resulting prompt lacks the skills index and most tool
guidance. The surface-switch path then adopted it as a legitimate earlier surface, so later
turns ran on the stripped prompt until the next full-agent compaction.

## Verification

- `tests/agent/test_system_prompt_restore.py::TestSurfaceSwitch::test_prompt_left_by_gateway_hygiene_compaction_is_rebuilt`
- Live: after a hygiene compaction, the next turn logs `stale runtime identity` rather than
  `switched surface gateway_hygiene -> <platform>`.

## Retirement

Drop this patch when upstream stops persisting a hygiene-agent prompt at the compaction
boundary or treats it as stale on restore.
