"""Replay shutdown spools under each owning gateway profile at startup."""

import logging
from pathlib import Path

from hermes_constants import get_hermes_home
from gateway.shutdown_flush import recover_pending_to_db

logger = logging.getLogger("gateway.run")


def recover_pending_shutdown_flush(runner) -> int:
    """Visit the launch home and every served home; leave failed spools for a later boot."""
    from gateway.run import _profile_runtime_scope

    launch_home = Path(get_hermes_home())
    homes = [launch_home, *((getattr(runner, "_served_profile_homes", None) or {}).values())]
    recovered = 0
    for home in dict.fromkeys(Path(home) for home in homes):
        try:
            with _profile_runtime_scope(home, prepared_secret_scope={}):
                recovered += recover_pending_to_db(
                    session_resolver=runner.session_store.resolve_session_id_for_key,
                )
        except Exception:
            logger.warning("Pending-message recovery failed for profile home %s; spool retained", home,
                           exc_info=True)
    return recovered
