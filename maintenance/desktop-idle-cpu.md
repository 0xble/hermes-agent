# Desktop idle session refresh cost

Fork patch identity: `desktop-idle-cpu`.

## Required behavior

Preserve the existing watcher cadence, metadata fields, heartbeat suppression,
read-only probes and failed-read fallback. Signatures are process-local: their
encoding is not a database or wire format. Keep binary doubles exact and avoid
object-reference-dependent serialization; never deserialize database input.

Recent session lists must choose their ordered page before transcript preview
projection. Compression-lineage recency, search filters, pagination and pinned
backfill retain their existing contracts. CR/LF normalization preserves character
length, so preview windows can be sliced first; legacy BLOB messages still use
text character boundaries.

## Provenance and adoption

This fork-only repair addresses the desktop backend's sustained idle load on a
large shared store: the 0.5-second watcher repeatedly rendered every metadata row,
while sidebar/project refreshes projected previews for discarded sort candidates
and normalized full messages. Config reads were already signature-cached.

Owning surfaces are `tui_gateway/change_watcher.py`, `hermes_state_common.py` and
`SessionDB.list_sessions_rich` in `hermes_state_sessions.py`. Deterministic cost
and semantic regressions live in `tests/tui_gateway/test_change_watcher_sessions.py`
and `tests/hermes_cli/test_session_skill_previews.py`; existing lineage, paging,
archive/pin and sidebar-cache tests cover the surrounding behavior.

Upstream disposition: no upstream PR or submission; this task stops at a draft
PR in `0xble/hermes-agent`. Fork delivery: this branch only, not installed.
During release reconciliation retain these cost bounds unless the adopted query
plan and signature implementation independently satisfy the regression tests.
Retire the patch when the accepted baseline supplies equivalent bounded work.
Rollback by reverting the commit carrying `Fork-Patch: desktop-idle-cpu`.
