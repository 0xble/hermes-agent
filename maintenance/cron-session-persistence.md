# Cron Session Persistence

Patch identity: `cron-session-persistence`.

## Required Behavior

Agent cron jobs acquire their own profile's session-store handle before running wake-gate scripts or monitors. Acquisition makes at most 3 attempts, each using the existing session-db timeout, with 50 ms between attempts. The existing nonpositive debugging override retains its unbounded single acquisition. Exhaustion records `session store unavailable; run not started` through the ordinary failure output, incident and delivery path. An abandoned acquisition still releases any late result.

The finite one-shot dispatch is already claimed before `run_job`. Its failed pre-start attempt refunds that claim during the owner-fenced outcome write and sets its retry instant 60 seconds ahead, so an outage beyond the original grace window cannot retire it. Recurring jobs retain their natural next tick without spending repeat budget. Wake-gate scripts and monitors do not run during the outage. All early returns release an acquired handle. Script-only `no_agent` jobs retain their existing execution and repeat behavior.

## Source and Adoption

Fork base: `9371a3ebcdffd3c4bcf44be49dbc7e3989feee44`. Upstream main inspected on 2026-10-13: `e06df9c2eae6901f4c88f464e0ae6e753c0428b8`. Its `cron/scheduler.py::_open_cron_session_db` still returns `None` and permits sessionless agents. No upstream contribution is part of this delivery. The scheduler owns this boundary because plugins cannot prevent pre-run acknowledgement or atomically preserve job counters.

Source surfaces: `cron/scheduler.py`, `cron/scheduler_session_store.py`, `cron/jobs.py`, `cron/job_run_retry.py`. Proof: `tests/cron/test_session_store_required.py` and `tests/cron/test_sessiondb_init_hang.py`, plus the cron suite. Preserve profile context propagation, late-result cleanup, failure routing, repeat refunds and script-only behavior together when adopting upstream changes.

Retire this divergence when an upstream release satisfies those contracts. Reverting it restores sessionless execution, including its persistence and caller-identity failure. This unit does not introduce shared gateway identity fallback or model-supplied session identity, and publication does not imply runtime adoption.
