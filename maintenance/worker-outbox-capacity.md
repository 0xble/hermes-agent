# Worker outbox capacity

Patch identity: `worker-outbox-capacity`.

## Independent hypothesis (2026-11-04)

The synchronous worker store admits only one outstanding operation. A successful
receipt removes that entry before another write starts. The failed cron's retained
`pending.json` is 350 bytes with an empty pending list and next sequence 1622;
it is not a backlog. Proactive prune failed at 02:51:28 with `outbox_full`.
`compression.archive` repeats compacted/held/CAS history in one operation, so a
legitimate snapshot can exceed the 4 MiB raw JSON bound. The exact rejected
payload was not retained; the operation is inferred from the prune call path.

The narrow fix is a lossless compressed representation for oversized journals,
retaining the existing 4 MiB disk bound, single outstanding operation, atomic
private writes, scope verification and identical receipt replay. Decode must be
bounded too. A definitely-unsent capacity rejection must leave the prior journal
and sequence usable rather than poisoning subsequent usage/lease/finalization.
Unknown outcomes and filesystem failures must remain sticky. Raising the disk
cap alone or dropping transcript/CAS fields would not solve the contract.

Native worker persistence is core behavior; configuration, plugins and hooks do
not own its atomic journal/receipt boundary. Source-only delivery is selected:
no runtime installation, restart, cron mutation or merge is authorized.

## Verification and lifecycle

Regression: `scripts/run_tests.sh tests/agent/test_worker_outbox_capacity.py
 tests/agent/test_runtime_session_store.py tests/agent/test_worker_outbox_reopen.py
 tests/agent/test_worker_outbox_adoption.py`.

Retire when the maintained baseline implements bounded lossless large-operation
journaling and safe pre-send rejection without changing receipt identity. Roll
back by reverting the commit with this patch identity; a compressed pending
journal must first be replayed by a compatible worker. Existing small JSON
journals remain readable and are not migrated proactively.

The first real-owner regression also exposed its separate 4 MiB UTF-8 payload
limit (`hermes_state_runtime.apply_worker_receipt`). Lossless journaling is only
useful for operations that owner accepts: JSON ASCII escaping/envelope overhead
can cross the disk bound before canonical UTF-8 does. Worker admission now checks
both the existing owner payload bound and the 8 MiB RPC frame bound before
journaling. Ineligible compaction retains its original transcript through the
existing best-effort caller; it does not advance sequence or poison the store.
No owner behavior, schema, payload identity or bound is changed.

Compressed decode is limited to min(2 * disk budget, 8 MiB), matching the existing
frame budget and preventing gzip bombs. Disk budget remains exactly 4 MiB by
default. Only journals whose raw representation does not fit are compressed.
Unknown outcomes, write/rename failures and ack-save failures remain fatal until
explicit identical receipt replay, as before. Oversized mandatory writes still
raise honestly; this is not an assertion of canonical persistence.

Prior art: upstream open PR #146198 addresses the unrelated on-loop refusal
poisoning boundary and deliberately retains outbox_full as sticky. No upstream
outbox-capacity fix or fork PR was found by exact-term search on 2026-11-04.

Contract tests: `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_outbox_codec.py`, `tests/agent/test_worker_outbox_io_failure.py`, `tests/agent/test_worker_prune_capacity.py`

Verification: RED on the unchanged baseline raised `outbox_full` on both large
snapshot paths and proved the failure latched; 3 tests failed. GREEN exercises
real-owner receipt deduplication after lost ack, compressed reopen, resource-bound
refusals, malformed journals, owner lock release, disk/ack-save errors and the
actual proactive-prune fallback followed by usage/lease persistence. This is
source delivery only; live incident data was read but never changed.
