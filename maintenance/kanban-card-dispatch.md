# Kanban card dispatch hardening

Fork patch identity: `kanban-card-dispatch`.
Contract tests: `tests/hermes_cli/test_kanban_host_cap.py`, `tests/hermes_cli/test_kanban_managed_worker_loss.py`.

## Required behavior

- `kanban.max_in_progress` caps running cards on this host at 12 by default (Brian's call on
  2026-11-27), overridable in config. It is counted across every board and profile the dispatcher
  ticks, and a missing or invalid value means the default. At the cap, ready cards stay queued and
  are not charged. This replaces the memory-derived default (MemTotal / 512 MiB clamped to [2, 8],
  uncapped where memory was unreadable). The memory-pressure guard still holds spawns back on a
  Linux host short of memory.
- A card attempt that its profile owner stops or restarts under is re-queued as `reclaimed` and
  never charged against `max_retries` / `kanban.failure_limit`. The evidence is the owner's own:
  a managed turn ended by the owner's system Stop exits `KANBAN_OWNER_STOP_EXIT_CODE` (79). A loss
  while the owner drains or retires, its own Stop included, and an in-flight card admission that
  boot recovery settles `interrupted` are recorded as `worker_lost` with `restart`. The crash
  sweep and the TTL reclaim both honor it. The first loss recorded for a run keeps its verdict, so
  a real crash before a restart is still charged. A user's Stop, a worker crash, a timeout, and
  rate-limit, billing or terminal provider exits keep their existing accounting.

## Scope and retirement

The source delta extends the existing `kanban.*` dispatch settings and dispatcher accounting. It
adds no configuration keys, schema or services. Retire a bullet when upstream ships the same
behavior. Rollback is a source revert.
