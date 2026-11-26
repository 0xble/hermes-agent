# MCP reload guidance

## Fork patch identity

This maintenance unit owns the fork patch identity `mcp-reload-guidance`.

## Behavior

The bundled `hermes-agent` skill's native MCP reference says the same thing as the user guide's Reloading section (`website/docs/user-guide/features/mcp.md`). A running gateway reconciles `mcp_servers` edits in `config.yaml` within about a minute, with no restart. An open session refreshes its tool set only on `/reload-mcp`, `/new`, or context compaction. The reference previously told agents to restart Hermes and said there was no hot-reload, which led agents to request needless gateway restarts.

## Source surfaces

- `skills/autonomous-ai-agents/hermes-agent/references/native-mcp.md`

## Upstream status

Upstream `main` still carries the stale restart wording in this reference, while its own user guide documents gateway reconciliation.

## Focused regression

Documentation only. Check that `grep -n "no hot-reload" skills/autonomous-ai-agents/hermes-agent/references/native-mcp.md` returns nothing, and that the reference matches the user guide's Reloading section.

## Retirement

Remove this patch when upstream corrects the reference to match the user guide.

## Rollback

Revert the commit carrying `Fork-Patch: mcp-reload-guidance`.
