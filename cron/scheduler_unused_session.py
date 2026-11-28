"""Close the session a cron fire owned when no agent turn ran."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# A fire that ended before any agent turn: a false wake gate (the common silent watcher tick), a
# failed or blocked script, or a blocked setup. Not an orphan reason, so boot never restores it.
NO_AGENT_END_REASON = "cron_no_agent"


def release_unused_session(session_db, job_id, owned_session_id, run_cleanup) -> None:
    """End the session the gateway pre-created for this fire, then release the store.

    Canonical fires run inside a session ``gateway.session_cron`` creates before the job's script
    gate decides. Without an agent nothing finalized that row, so it stayed open with no messages
    until the idle or startup orphan reaper closed it, and every boot restored it meanwhile (about
    86% of cron sessions on 2026-11-28). Legacy fires (``owned_session_id`` None) own no row yet.
    """
    from hermes_state_registry import release_or_close

    def cleanup():
        if owned_session_id:
            try:
                session_db.end_session(owned_session_id, NO_AGENT_END_REASON)
            except Exception as exc:  # bookkeeping must never replace the run's result
                logger.debug("Job '%s': failed to end unused cron session: %s", job_id, exc, exc_info=True)
        release_or_close(session_db)

    run_cleanup(cleanup, job_id=job_id, label="unused session store release")
