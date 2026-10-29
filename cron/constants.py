"""Fire-claim timing bounds shared by the job store and its siblings.

Import-free on purpose: a sibling loaded fresh from a newer on-disk tree must resolve
these without going through the ``cron.jobs`` object a long-lived process cached at boot.
"""

# A fire_claim younger than this is a live run (heartbeat cadence is 60 s). One value
# for claiming, one-shot re-arm, and stale-error recovery so they cannot disagree.
FIRE_CLAIM_TTL_SECONDS = 300
# A hosted/webhook fire for the armed slot can arrive a few seconds before the stored
# ``next_run_at`` (the fire scheduler's clock runs ahead of ours). Claims that early still own
# the slot; only claims further ahead are off-tick manual/dashboard fires.
FIRE_CLAIM_SKEW_SECONDS = 60
# Multiplier over HERMES_CRON_TIMEOUT for claim TTLs: the timeout is an *inactivity* limit, not a
# wall-clock cap, so healthy runs may legitimately exceed it and a TTL of exactly the timeout
# would expire live claims.
CLAIM_TTL_INACTIVITY_HEADROOM = 3


PROTECTED_INTERVAL_MAX_SECONDS = 120
PROTECTED_INTERVAL_HEADROOM_SECONDS = 30


def is_protected_interval(job: dict) -> bool:
    """Short interval schedules share one reserved worker, independent of job names or IDs."""
    schedule = job.get("schedule") or {}
    if not isinstance(schedule, dict) or schedule.get("kind") != "interval":
        return False
    try:
        return 0 < float(schedule.get("minutes", 0)) * 60 <= PROTECTED_INTERVAL_MAX_SECONDS
    except (TypeError, ValueError):
        return False


def is_recurring(job: dict) -> bool:
    """A cron/interval job (vs a one-shot); a null ``schedule`` counts as not recurring."""
    return (job.get("schedule") or {}).get("kind") in {"cron", "interval"}
