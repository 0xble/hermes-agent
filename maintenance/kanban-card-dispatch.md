# Kanban card dispatch hardening

Fork patch identity: `kanban-card-dispatch`.
Contract tests: `tests/hermes_cli/test_kanban_host_cap.py`, `tests/hermes_cli/test_kanban_managed_worker_loss.py`, `tests/gateway/test_kanban_notifier.py`, `tests/gateway/test_kanban_result_exit.py`.

## Required behavior

- `kanban.max_in_progress` caps running cards on this host at 12 by default (Brian's call on
  2026-11-27), overridable in config. It is counted across every board and profile the dispatcher
  ticks, and a missing or invalid value means the default. At the cap, ready cards stay queued and
  are not charged. This replaces the memory-derived default (MemTotal / 512 MiB clamped to [2, 8],
  uncapped where memory was unreadable). The memory-pressure guard still holds spawns back on a
  Linux host short of memory.
- A card attempt that its profile owner stops or restarts under is re-queued without a charge
  against `max_retries` / `kanban.failure_limit`, for up to `OWNER_RESTART_LIMIT` (3) such runs in
  a row, and the next one is charged. The evidence is the owner's own: its Stop control ending the
  turn makes the worker exit `KANBAN_OWNER_STOP_EXIT_CODE` (79), never an EOF or a malformed
  control frame. A worker the owner Stopped for its restart or shutdown (`stop_cause`), and an
  in-flight card admission that boot recovery settles `interrupted`, are recorded as `worker_lost`
  with `restart`, before the admission settles. The crash sweep, the TTL reclaim and the runtime
  timeout all honor it and mark the run `owner_restart`. The first loss recorded for a run keeps
  its verdict, and a loss during a drain that the owner did not cause is charged. A user's Stop,
  a worker crash, a plain timeout, and rate-limit, billing or terminal provider exits keep their
  existing accounting.
- A `completed` or `review_requested` wake carries the run's full summary (the task result for
  legacy rows), up to 16,000 characters with a pointer to `hermes kanban show` for the rest, and
  its guidance precedes the handoff. The event payload keeps its 400-character first line, which
  keeps board events and the dashboard stream small, and the chat ping keeps its one-line preview.
  A failed run read falls back to the event's summary.

## Scope and retirement

The source delta extends the existing `kanban.*` dispatch settings and dispatcher accounting. It
adds no configuration keys, schema or services. Retire a bullet when upstream ships the same
behavior. Rollback is a source revert.
