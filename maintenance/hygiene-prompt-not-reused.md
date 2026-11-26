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

Upstream [#124158](https://github.com/NousResearch/hermes-agent/pull/124158) and
[#124200](https://github.com/NousResearch/hermes-agent/pull/124200) merged by
2026-09-26. They preserve the seeded prompt during detached compaction and add
`hermes sessions repair-prompts` for historical damage. Track them as native
replacements, with release adoption and historical repair verified separately.

The fork adopts both merged implementations: detached hygiene and manual compression
retain the exact seeded prompt and tool pin, while normal compaction still refreshes.
The repair command defaults to reporting. Automatic application needs positive
`skill_manage` pin evidence and skips missing, malformed, or memory-only pins.
Explicit session selection retains upstream's documented override. The fork additionally
compares the scanned prompt and pin atomically before clearing, preserving live replacements.
This conditional repair is proposed upstream in
[PR #125569](https://github.com/NousResearch/hermes-agent/pull/125569).
Source adoption does not assert that any production historical row has been repaired.

## Verification

- `tests/agent/test_system_prompt_restore.py::TestSurfaceSwitch::test_prompt_left_by_gateway_hygiene_compaction_is_rebuilt`
- `tests/gateway/test_hygiene_compaction_keeps_seeded_prompt.py`
- `tests/hermes_cli/test_sessions_repair_prompts.py`
- `tests/hermes_cli/test_sessions_repair_prompt_race.py`
- Live: after a hygiene compaction, the next turn logs `stale runtime identity` rather than
  `switched surface gateway_hygiene -> <platform>`.

## Retirement

Drop the restore guard only after upstream prevention and historical repair have both
been verified on the selected release. A prevention fix alone does not repair old rows.
