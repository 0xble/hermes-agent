# Bounded delegation notices

Load this unit when changing how async-delegation completion notices render
the dispatch context (`tools/process_registry_notifications.py::_preamble`).

## Required behavior

- A completion notice echoes at most the first 2,000 characters of the
  dispatch context, followed by an omitted-size note. The parent already holds
  the full context in its own transcript.
- The result, status, and task headers are unaffected. Summaries keep their
  existing `delegation.max_summary_chars` budget.

## Why

An unbounded echo turned a 942 KB review diff into a 947 KB user turn. As the
newest user turn it is protected from summarization, so compression reported
`no_progress` and entered structural backoff while the session sat above the
threshold. The same echo produced 95 KB to 947 KB notices in seven sessions
between 2026-09-19 and 2026-09-24.

## Provenance

Fork patch identity: `bounded-delegation-notices`.

Upstream-owned code (`d4cec15b47e`). No upstream issue or PR covered it when
this patch landed. Contribute the same change upstream.

## Verification

Run `scripts/run_tests.sh tests/tools/test_async_delegation.py -k oversized_task_source`.
It dispatches real single and batch background delegations with a 1.2 MB
context and asserts the notice keeps the result and context head but not the
bulk.

## Retirement and rollback

Retire when upstream bounds the echoed context. To roll back, revert the patch
commit; no state or configuration is involved.
