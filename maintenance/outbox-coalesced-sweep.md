# Outbox coalesced deferred sweep

Load this unit when changing `gateway/outbox.py` retry scheduling, `recover()`,
receipts, held-row reporting, or the store's connection handling.

**Patch identity:** `outbox-coalesced-sweep`.

## Required behavior

- Deferred redelivery is one sweep per `(store path, platform, profile)`. The sweep
  sleeps to the store's earliest `retry_at`, runs one `recover()`, and repeats until
  no deferred row remains. Scheduling while a sweep is parked wakes it to reread the
  earliest deadline. A failed pass backs off for 5, 15, 30, then 60s. A pass that
  claims nothing waits at least 1s, so the sweep never spins.
- `recover()` is serialized per store within each event loop.
- A held row (`sending` or `ambiguous`) is logged once per process, and again only
  after it leaves and re-enters the held set. A row this process is dispatching right
  now is in flight, not held, and is never reported.
- Every store operation closes its SQLite connection. `with sqlite3.Connection` only
  commits, and an unclosed connection kept its db/WAL/SHM descriptors until GC (#69567).
- A send's known outcome is never lost to a transient local store failure. Receipts,
  `begin_send` and row preparation retry descriptor exhaustion, an unopenable file,
  and lock or I/O errors for about 16s. A receipt that stays unwritable leaves only
  that row held (never resent), logs once, and the pass continues.
- A transport failure caused by descriptor exhaustion (an `OSError` EMFILE/ENFILE in
  the exception chain, or "Too many open files" in a `SendResult` error) is proven
  unsent, not ambiguous. Opening the socket or file the request needed failed.

## 2026-10-05 Incident

The Telegram DM's 6.7h flood penalty lifted at 02:58:45 PDT. Every row deferred
during it had the same `retry_at`, and `_schedule_retry` had made one `redeliver`
task per row. Hundreds of tasks woke at once, each running a full `recover()`.
Every pass that finished logged every row in `sending`, including rows other
passes were still dispatching. That produced 483k `Held ambiguous` lines between
03:03 and 03:20, and rotated `gateway.log` three times in six minutes. Each store
call leaked a connection until GC. A reproduction with 200 rows sharing one deadline
leaked 70,563 descriptors with GC disabled, so the gateway hit EMFILE at 03:04 and
03:20. During both windows flood-state reads failed, the adapter refused queued
sends locally on its 60s fail-closed path, and the receipts for those refusals hit
EMFILE. 139 rows (134 redeliveries and 5 live finals) were left in `sending`, each
in the same second as a local "refusing locally without an API call" refusal. The
classification of `flood_control:<s>` as a timed deferral (archived HERMES-085) is
native. Its coalesced sweep was not, and this unit restores it.

**Independent hypothesis.** A redelivery identity is the store and adapter, not the
row, so N rows sharing a deadline need one pass. The log, descriptor and receipt
failures were all amplified by N concurrent passes, plus the connection leak that
made each pass cost descriptors. Alternatives: a semaphore around per-row tasks
still runs N passes, each reporting every held row. Raising `NumberOfFiles` hides
the leak. Rate-limiting the log line still loses receipts.

**Upstream.** No upstream outbox exists. This store is fork-only (seamless restart
S4.1), so there is no upstream issue or PR to track.

**Regression:** `scripts/run_tests.sh tests/gateway/test_outbox_coalesced_sweep.py`.
It covers 200 rows sharing a deadline: one sweep, one `recover()` pass, all
delivered, descriptor growth under 20 with GC disabled, and one log line per held
row across passes. It also pins serialized concurrent `recover()` calls, in-flight
rows never reported held, a receipt surviving transient EMFILE, an unwritable
receipt holding only its row, descriptor exhaustion as unsent, an earlier deadline
waking the parked sweep, and a sweep surviving a failed pass. On the pre-patch base,
the same 200-row scenario ran 200 passes, logged 634 held lines and leaked 70,563
descriptors.

**Rollback:** Revert the `fix(gateway): coalesce deferred outbox redelivery into one
sweep` commit. It involves no schema, state or configuration change, and N-1
releases share the store unchanged.

**Retirement:** Retire with the outbox itself, or when redelivery moves to a
scheduler that owns one timer per store.
