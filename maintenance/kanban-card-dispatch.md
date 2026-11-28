# Kanban card dispatch hardening

Fork patch identity: `kanban-card-dispatch`.
Contract tests: `tests/hermes_cli/test_kanban_host_cap.py`.

## Required behavior

- An unset `kanban.max_in_progress` caps running cards on every host whose total memory is
  readable: `/proc` on Linux, `sysconf` on macOS. The derived cap is MemTotal / 512 MiB clamped to
  [2, 8], counted across every board and profile the dispatcher ticks. At the cap, ready cards stay
  queued and are not charged. The memory-pressure guard still needs MemAvailable, so it stays
  inactive on macOS.

## Scope and retirement

The source delta extends the existing `kanban.*` dispatch settings and dispatcher accounting. It
adds no configuration keys, schema or services. Retire a bullet when upstream ships the same
behavior. Rollback is a source revert.
