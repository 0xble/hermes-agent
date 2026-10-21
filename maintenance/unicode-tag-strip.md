# Unicode TAG character stripping

## Fork patch identity

This maintenance unit owns the fork patch identity `unicode-tag-strip`.

## Behavior

Model-facing results from `skill_view`, `web_extract`, `web_search`, `browser_*`,
`mcp_*`, and `read_file` have invisible Unicode TAG characters removed through
the existing `strip_unicode_tags` helper. Valid emoji tag sequences survive.
Clean text is returned unchanged, including object identity. `skill_view` and
`read_file` are not added to the untrusted-wrapping set. File edits still match
disk content, not the displayed `read_file` string, so stripping a result cannot
change a later patch match.

## Source surfaces

- `agent/tool_dispatch_helpers.py`
- `tests/agent/test_tool_dispatch_helpers.py`

## Upstream status

Upstream already strips TAG characters from MCP tool results, resource content,
and tool descriptions. Related design: [skills-guard Unicode TAG detection](https://github.com/NousResearch/hermes-agent/pull/6135)
detects the channel at skill-install time and does not strip loaded skill,
web, browser, or file results. No equivalent model-facing strip was found.

## Focused regression

`scripts/run_tests.sh tests/agent/test_tool_dispatch_helpers.py tests/tools/test_unicode_tag_strip.py`

## Retirement

Remove this patch when released upstream strips the same tool-result paths
through one shared helper and preserves emoji tag sequences.

## Rollback

Revert the commit carrying `Fork-Patch: unicode-tag-strip`.
