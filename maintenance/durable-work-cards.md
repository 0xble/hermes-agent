# Durable work routes to Kanban cards

Fork patch identity: `durable-work-cards`.

Deliberate fork-only patch, chosen by Brian on 2026-11-27. Background delegations end with the
gateway, so `delegate_task`'s guidance sends durable work to a Kanban card keyed to its task
instead of cron or a background terminal. The fork's "How Work Flows" instructions carry the
same rule; the tool description must not contradict them.

## Required behavior

- The `delegate_task` description says: work someone would have to remember to redo if it were
  lost (anything that changes the world, runs over about 20 minutes, or must not be dropped)
  goes to a Kanban card keyed to its task (`hermes kanban create --idempotency-key task:<id>
  --goal`); a scheduled job goes to `cronjob`.
- Quick, disposable work whose result only matters to the conversation stays a delegate.

## Source surfaces

`tools/delegate_tool.py` (`_DESCRIPTION_HEAD`) and
`tests/fixtures/prompt_surface/tools/delegate_task.json`.

## Retirement

Retire when upstream's delegate guidance names Kanban cards for durable work, or when the fork
stops using Kanban cards for durable work.
