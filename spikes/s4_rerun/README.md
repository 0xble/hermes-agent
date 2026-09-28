# S4.2 rerun: component crash-window spike

This is a disposable experiment, **not** a router/executor implementation. `probe.py` drives the real S4.1 outbox decorator on `TelegramAdapter.send` through python-telegram-bot to a localhost Telegram API stub. It SIGKILLs an egress worker after the stub accepts a send and before a platform receipt can commit; `Outbox.recover` must hold the uncertain row rather than resend it. A second probe exercises `gateway.status.acquire_scoped_lock`, with an argv marker to meet its gateway-process identity check. The marker is **not** an actual router and there is no `getUpdates` poller.

Run focused tests with `scripts/run_tests.sh tests/gateway/test_s4_rerun.py -q`. For persistent evidence, invoke `run_crash_windows(home / "batch-unique", home / "evidence", 20)` with an isolated `HERMES_HOME` and use `run_scoped_lock_conflict(home)`. Do not reuse an existing batch directory: its already-held outbox row intentionally suppresses subsequent sends. The full seven-item assessment and 20-run receipts live in the caller-owned scratch report at `~/.hermes/cache/scratch/seamless/p2/s4-2-rerun-report.md`.

## Verdict: PARTIAL

- Native S4.1 ambiguity handling held 20 accepted sends with no duplicates after egress-process death.
- No native router/executor boundary, release-pinned executor, approval/delegation forwarding, busy queue, streamed edit continuity, or actual poller transfer was proved.
- Recommendation: **STOP** S4.2 implementation approval. Do not merge this draft spike.
