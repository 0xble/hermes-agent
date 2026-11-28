# Retired reviewed cron delivery

Patch identity: `reviewed-cron-delivery`.
Fork-Patch-Backfill: b7f8c5a8993701ce0c41027c80c5d2737ea9f78e; reviewed-cron-delivery

Retired on 2026-11-27 because the feature was unused: none of the 465 jobs in the live
`cron/jobs.json` had a truthy `review` declaration, the last review receipt in
`cron/review-receipts.jsonl` dates from 2026-10-26, and the delivery queue held no `reviewing`
rows and no queued job carrying a review.

Removed: the scheduler review state machine (`cron/scheduler_review.py`), the contract checker and
receipts (`cron/review.py`), the rerun child (`cron/review_rerun.py`), the bundled report contracts
and their package data, the gateway control-socket review verbs and invisible capture
(`gateway/cron_review.py`) with their busy, drain, shutdown and resume exclusions, the
`--review-json`/`--clear-review` CLI flags and `review_json`/`clear_review` tool arguments, the
`cron.review.excluded_threads` default, the user-guide section and the exclusive tests.

Legacy state: a job that still carries a `review` key, including `review: null`, loads and runs as
an ordinary job, and the key is ignored. A leftover `cron.review` block in `config.yaml` is
ignored. The `deliveries` table keeps `reviewing` in its status check. Recovery returns any such
row to `pending` with its original content, and it is then delivered normally.

Kept: `keep_pinned_lane`, `output_local_only` and the `cron.wrap_response: false` fallback-notice
behavior are used by live jobs and configuration. They now belong to
[Cron local delivery hint](cron-local-delivery-hint.md). The fallback pause-reason persistence and
the replay row-version fix that once carried this trailer also stay. This record stays so
published `Fork-Patch` trailers keep an accountable owner.

Contract tests: none, retired.
